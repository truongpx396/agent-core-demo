"""The graph's state schema and the numbers that bound a turn.

A leaf module: it imports nothing from the rest of `app/agent`, so every node,
router and builder can depend on it without a cycle.

- `State` — everything the graph carries between nodes and across turns, and
  `concat_or_reset`, the one custom reducer behind it.
- `STATE_SCHEMA_VERSION` — bumped only on a genuinely incompatible `State` or
  topology change (see its comment).
- The safety budgets (iterations, tokens, tool calls per turn, history size,
  repeat and retry limits) that `should_continue`, `check_output` and the
  compaction node enforce, and the smaller ones a nested subagent run is held
  to. Their reasoning lives in the comments next to each, and in
  GRAPH_PATTERNS.md patterns 10, 13, 34, 35 and 41.
"""
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.core.security import SecurityCtx

MAX_ITERATIONS = 10  # safety budget: LLM loop iterations, per turn (see validate_input's reset)
MIN_ANSWER_LENGTH = 10
MAX_TOOL_CALLS_PER_TURN = 5  # safety budget: simultaneous tool calls from one LLM turn
MAX_TOKENS_PER_TURN = 16000  # safety budget: cumulative token usage per
# turn (0 if the model/proxy doesn't report usage_metadata — fails open).
# Bumped from 8000 after a live bug: a citation-repair retry round roughly
# doubles a turn's token spend, so 8000 could cap an otherwise-good,
# correctly cited answer before check_output ever saw it (caught via
# Langfuse). 16000 leaves retry headroom without approaching num_ctx
# (32000) — see HISTORY_TOKEN_CEILING's comment for why input headroom
# isn't scaled 1:1 with num_ctx either.
HISTORY_TOKEN_CEILING = 24000  # bounds the only unbounded input in State
# (see _trim_history) — trips compact_history once raw (non-system)
# history exceeds this estimated token count.
HISTORY_TOKEN_FLOOR = 4000  # once tripped, trim front-to-back until the
# kept tail is at/under this — deliberately LOWER than the ceiling
# (hysteresis, not a sliding window of 1). A prior fixed-turn-count design
# re-triggered compact_history's own summarization call every turn past
# the threshold, and reshifted the kept history's position each time (bad
# for prefix-cache reuse). A lower floor buys several turns of stable,
# cache-friendly growth before the ceiling trips again.
#
# Not scaled 1:1 with litellm-config.yaml's num_ctx (32000): num_ctx
# answers "what fits without truncation" (needs margin for ~2500 tokens of
# system prompt/tool schemas plus a retry round's ~2x spend); this pair
# answers "how much raw history is worth carrying" — a cost/latency
# tradeoff num_ctx headroom doesn't resolve. qwen2.5:3b has been caught
# dropping the "mandatory" citation instruction in prompts of only
# ~2300-2800 tokens, so a wide ceiling trades cheaper/rarer compactions
# against more context for a small model to attend to — not sized up just
# because num_ctx has room. 24000/4000 leaves margin below num_ctx for a
# retry round plus retrieved context/tool results.
MAX_HISTORY_SUMMARY_CHARS = 4000  # (AR-015a) the CUMULATIVE history_summary
# must stay bounded too, or the "compacted" summary becomes the next
# unbounded input. Exceeding it is a named terminal state
# (context_window_exceeded), not silent truncation — see route_after_compaction.
MAX_REPEATED_ACTIONS = 3  # consecutive IDENTICAL tool-call batches before
# ending as no_progress — bounds convergence, not just repetition, and
# fires independently of (usually before) MAX_ITERATIONS — see should_continue.
MAX_CONSECUTIVE_SAME_RETRY_REASON = 2  # check_output's own convergence
# check, mirroring MAX_REPEATED_ACTIONS but for the retry_output loop.
# Live bug: a turn stuck on the SAME rejection reason burned 6 retry
# rounds (~18k tokens) before MAX_TOKENS_PER_TURN cut it off, landing on
# the same fallback it could've reached after 2. `2` = one
# self-correction attempt per distinct reason; deliberately does NOT reset
# on a DIFFERENT reason (that's slow progress through distinct issues, not
# a stuck loop) — see route_after_check/retry_exhausted.

# Budgets for a NESTED subagent run (app/agent/tools/run_subagent_domain.py::make_domain_subagent_tool,
# pattern 46) — separate from the constants above; a subagent's own
# should_continue is bound to THESE via functools.partial, independent of
# the parent turn's remaining budget. Hardcoded (not Settings-backed) as a
# loop-count safety net, unlike MAX_SUBAGENT_COST_USD_PER_RUN
# (app/core/config.py, IS Settings-backed — a $ policy knob, not a safety net).
MAX_SUBAGENT_ITERATIONS = 6
MAX_SUBAGENT_TOKENS_PER_RUN = 4000


# Bump only on a genuinely incompatible State/topology change (a renamed/
# removed State key, or a removed/reordered node a *paused* thread might
# resume into) — an ordinary change (new node appended, prompt/timeout
# tweak) leaves this unchanged deliberately. Meaningful only with a durable
# checkpointer (app/agent/runtime/core.py's AsyncPostgresSaver) — MemorySaver
# never survives a restart, so there's never a stale checkpoint to compare
# against.
STATE_SCHEMA_VERSION = 1


def concat_or_reset(
    current: list[tuple[int, float]] | None, update: list[tuple[int, float]] | None
) -> list[tuple[int, float]]:
    """Reducer for `State.subagent_spend`: list-concatenate, or reset on `None`.

    Why not plain `operator.add`: ToolNode runs a turn's tool calls
    CONCURRENTLY, so several `run_subagent` calls each append one entry via
    `Command(update=...)`, and a concatenating reducer is what makes that
    race-free. But a concatenating reducer also makes "reset to []" a no-op —
    `validate_input` writing `[]` just adds nothing — so every earlier turn's
    delegated spend silently kept counting toward later turns' token/cost
    ceilings (a bug reproduced before this reducer existed, and one the old
    unit test could not see because it asserted `validate_input`'s RETURN
    value, not the state the reducer produced from it). `None` is the
    explicit reset marker: it is what `validate_input` writes, and it is
    never stored (the reducer returns a list). No `Overwrite` exists in the
    pinned langgraph, hence this.
    """
    if update is None:
        return []
    return list(current or []) + list(update)


class State(TypedDict):
    """Richer state: track messages, context, and flow control."""

    messages: Annotated[list[BaseMessage], add_messages]
    context: str  # Enriched context from search (set by retrieve_context).
    iterations: int  # Track how many agent loops we've done *this turn*.
    total_tokens: int  # Cumulative token usage *this turn* (see agent()).
    total_cost_usd: float  # Cumulative $ cost *this turn*, priced per call from
    # the model's LiteLLM prices (app/agent/cost/pricing.py) — should_continue enforces
    # MAX_COST_USD_PER_TURN against this
    # (GRAPH_PATTERNS.md patterns 26 and 35).
    subagent_spend: Annotated[list[tuple[int, float]], concat_or_reset]  # One
    # (tokens, cost_usd) entry per completed run_subagent call this turn,
    # appended via Command(update=...) from app/agent/tools/run_subagent.py's run_subagent. The
    # only other reducer field besides `messages`: ToolNode runs multiple
    # tool calls from one AI turn CONCURRENTLY (pattern 9), so concurrent
    # run_subagent calls must list-concat safely rather than race a
    # read-modify-write like total_tokens/total_cost_usd do (safe there
    # only because `agent` never runs concurrently with itself). Summed
    # into should_continue's MAX_TOKENS_PER_TURN/MAX_COST_USD_PER_TURN
    # checks on top of total_tokens/total_cost_usd (pattern 46's disclosed
    # gap), without touching MAX_SUBAGENT_*'s own separate per-run ceiling.
    # Reset every turn by validate_input (it writes `None`, which
    # `concat_or_reset` turns into []), same as total_tokens/
    # total_cost_usd — unlike history_summary, must NOT accumulate turn over turn.
    require_approval: bool  # Opt-in: gate tool calls behind human_approval.
    approved: bool  # Set by human_approval; read by route_after_approval. Reset
    # every turn by validate_input (it is not re-run on a resume, so a pause's
    # own decision is never cleared out from under it).
    cancelled: bool  # Set by human_approval on a cancel decision; read by
    # route_after_approval to end the run outright (GRAPH_PATTERNS.md pattern 36).
    run_id: str  # Per-turn correlation id for node lifecycle logs (see
    # validate_input, instrumented) — regenerated every turn, same reset
    # point as iterations/total_tokens.
    graph_version: str  # Build that wrote this checkpoint — see _graph_version.
    state_schema_version: int  # See STATE_SCHEMA_VERSION / resumability_error.
    ctx: SecurityCtx | None  # Stamped ONCE by validate_input from
    # config["configurable"]["ctx"] (the trusted boundary) — read-only
    # from here on; no other node may write this key. See
    # app/core/security.py's SecurityCtx and route_after_validation's
    # fail-closed check.
    citations: list[dict]  # Set by retrieve_context — every numbered [n]
    # source retrieve_context's pre-fetch could have cited, whether or not
    # the final answer actually used it. See app/agent/tools/builtin.py::gather_context.
    used_citations: list[dict]  # Set by check_output — `citations` filtered
    # down to the markers that actually appear in the final answer text
    # (GRAPH_PATTERNS.md pattern 20). What the streamed `citations` SSE event carries.
    ungrounded_claims_count: int  # Set by check_output — [n] markers the
    # answer used that don't match any real citation (GRAPH_PATTERNS.md pattern 39).
    output_findings: dict[str, Any]  # Set by check_output — every output check that fired on
    # this round's answer, `{code: detail}` in priority order (an empty dict when it passed).
    # The detail is what that check's feedback needs: the offending citation markers for
    # "uncited"/"misattributed", the skipped tool's NAME for "skipped_tool", `True` otherwise.
    # Replaces what used to be one State flag per check. The checks, their priority, feedback
    # and fallback text are the registry in graph/output_checks.py. Reset every turn.
    last_retry_reason: str | None  # Set by check_output — short code
    # naming this round's rejection reason (the first key of `output_findings`:
    # "leaked_prompt"/"too_short"/"fabricated"/"skipped_tool"/"deferred"/
    # "uncited"/"misattributed", in graph/output_checks.py's priority order), or None. Compared against the
    # PRIOR round's value by check_output to compute
    # retry_reason_repeat_count below — never reset mid-turn elsewhere.
    retry_reason_repeat_count: int  # Set by check_output — consecutive
    # rounds THIS SAME reason has fired (1 on first occurrence, reset to 1
    # on a different reason). Read by route_after_check to give up
    # (routing to retry_exhausted) once MAX_CONSECUTIVE_SAME_RETRY_REASON is hit.
    cache_hit: bool  # Set by check_semantic_cache — read by
    # write_semantic_cache to skip a redundant re-embed+write on a turn that
    # was already served from cache (GRAPH_PATTERNS.md pattern 22).
    moderation_blocked: bool  # Set by moderate_input — read by
    # route_after_moderation (GRAPH_PATTERNS.md pattern 25).
    followups: list[str]  # Set by suggest_followups — 2-3 follow-up
    # questions derived from a grounded answer, or [] when the answer had
    # no citations to derive them from (GRAPH_PATTERNS.md pattern 27).
    history_summary: str  # Set by compact_history — cumulative summary of
    # what _messages_to_trim has discarded, across the WHOLE thread's
    # lifetime. NOT reset per-turn (unlike citations/followups) — it
    # accumulates like the checkpointed message list itself. Injected by
    # agent() as an early SystemMessage (pattern 41).
    context_anchor_index: int | None  # Set by retrieve_context, alongside
    # `context` — the index, in THAT MOMENT's state["messages"], of the
    # human message that opened this turn. Only appends happen after this
    # point for the rest of the turn, so the index stays valid throughout
    # the agent<->tools/check_output<->retry_output loop. agent() re-locates
    # this fixed position on every call to splice history_summary/context
    # right before the turn's question, rather than at the current tail —
    # see agent()'s own docstring for why a SHIFTING position broke
    # prefix-cache reuse. `None` = unset: validate_input resets it (and
    # `context`) every turn, so a turn that never reaches retrieve_context —
    # a semantic-cache hit that then fails check_output — can't splice the
    # PREVIOUS turn's context at the previous turn's index. agent() appends
    # at the tail when it is missing.
