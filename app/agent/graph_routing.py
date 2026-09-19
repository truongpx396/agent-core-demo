"""The two conditional-edge gate functions — `should_continue` (after
`agent`: tool call, final answer, or safety budget?) and `check_output`
(is the final answer good enough to end the turn on?) — plus
`route_after_check`, the routing function consuming `check_output`'s own
output. Split out of `app/agent/graph.py` purely for file size; `State`,
every node function/factory, and the other routing functions
(`route_after_validation`, `route_after_moderation`, `route_after_cache`,
`route_after_compaction`, `route_after_approval`) stay there since they're
tied to nodes that stay there too. See `app/agent/graph.py`'s own module
docstring, and `app/agent/graph_build.py`/`app/agent/graph_build_subagent.py`
for the two graph-assembly functions that wire both gates below into a
compiled graph. No behavior change from the pre-split single-file version.

Each gate's own private helper cluster now lives in its own sibling file,
split out of this one purely for file size — `app/agent/graph_loop_guards.py`
(`should_continue`'s own helpers: tool-call fingerprinting/repeat
detection, the mandatory-approval capability gate, the
use_skill-without-search check) and `app/agent/graph_citations.py`/
`app/agent/graph_output_guardrails.py` (`check_output`'s own two helper
clusters: citation-grounding checks, and the
deferral/fabrication/prompt-leak heuristics plus their priority
arbitration). `should_continue`/`check_output`/`route_after_check`
themselves stay here, at their original import path, so every existing
caller (`app/agent/graph_build.py`, `app/agent/graph_build_subagent.py`,
`app/agent/graph.py`'s own deferred imports, and every test that does
`graph_routing.check_output(...)`/`graph_routing.should_continue(...)`)
is unaffected by this further split.

A handful of helpers `should_continue`/`check_output` depend on
deliberately did NOT move here (or into the sibling files above), even
though they look like they belong — each is also called directly by a
node function that stays in `app/agent/graph.py`
(`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES` by the
`invalid_tool_call` node, `app/agent/graph_tools.py`; `_current_turn_messages`
by `make_agent_node`, also `graph_tools.py`; `_pending_skill_required_tool`
by `make_agent_node`, `app/agent/graph_skills.py`; `_content_words` by
`_retrieval_query`, `app/agent/graph_utils.py`), so moving them INTO
`graph.py` would force it to import back from here — a real circular
import, avoided by each living in its own sibling file instead, imported
from there below (never from `graph.py` directly for these five).
"""
from collections.abc import Mapping
from typing import Literal, cast

from langchain_core.messages import AIMessage
from langgraph.prebuilt import tools_condition

from app.agent.graph import (
    MAX_CONSECUTIVE_SAME_RETRY_REASON,
    MAX_ITERATIONS,
    MAX_REPEATED_ACTIONS,
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    SYSTEM_PROMPT,
    State,
)
from app.agent.graph_citations import (
    _insert_missing_citation_markers,
    _likely_misattributed_citations,
    _likely_uncited_citations,
    _used_citations,
    _ungrounded_claims_count,
)
from app.agent.graph_loop_guards import (
    _consecutive_repeat_count,
    _mandatory_gate_reason,
    _use_skill_called_without_search,
)
from app.agent.graph_output_guardrails import (
    _defers_instead_of_acting,
    _fabricates_tool_output,
    _leaks_system_prompt,
    _retry_reason,
    _skipped_required_sandbox_after_skill,
    _strip_fabricated_reference_footer,
)
from app.agent.graph_tools import _DEFAULT_VALID_TOOL_NAMES, _invalid_tool_call_names
from app.agent.tools import TOOL_CAPABILITIES
from app.core import metrics
from app.core.config import MAX_COST_USD_PER_TURN


# --- Edge fn: after agent, route to tools / output check / abort ---
def should_continue(
    state: State,
    tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES,
    valid_tool_names: frozenset[str] = _DEFAULT_VALID_TOOL_NAMES,
    max_iterations: int = MAX_ITERATIONS,
    max_tokens: int = MAX_TOKENS_PER_TURN,
    max_cost_usd: float = MAX_COST_USD_PER_TURN,
) -> Literal[
    "tools",
    "human_approval",
    "too_many_tool_calls",
    "invalid_tool_call",
    "use_skill_without_search",
    "check_output",
    "no_answer",
]:
    """Did the LLM call a tool, give a final answer, or hit a safety budget?

    Checked in order: the iteration cap and token cap are turn-wide safety
    nets (checked first, regardless of what the LLM just said); then
    whether this is a tool call at all; then whether it's *too many* tool
    calls at once; then whether any of them isn't a real registered tool at
    all (`invalid_tool_call` — see `_invalid_tool_call_names`); then whether
    `use_skill` was called without `skill_search` earlier this turn
    (`use_skill_without_search` — see `_use_skill_called_without_search`);
    then whether it needs human approval before running. Both the
    invalid-name and use_skill-without-search checks run BEFORE the
    human-approval gate deliberately: neither is a real tool call anyone
    could meaningfully approve or reject (a malformed name outright, or a
    real tool called on a guessed argument the model was explicitly told
    to look up first), so both are rejected and looped back to `agent` for
    a self-correcting retry instead of ever reaching a human with garbage
    to review — `use_skill` is read_only anyway (never reaches
    human_approval on its own merits), but the ordering still matters for
    consistency with the invalid-name check right above it.

    Two independent reasons route to `human_approval`, and only one of them
    is optional:
    - `require_approval` on the input state — opt-in (default False), so
      the existing CLI/API behavior is unchanged unless a caller asks for
      it. See app/channels/chat.py's `--hitl` mode.
    - Any pending tool_call whose declared capability (`tool_capabilities`
      — app/agent/tools.py::TOOL_CAPABILITIES by default, or a domain's own
      mapping, see below) isn't "read_only" — mandatory, never skippable
      via `require_approval=False`. A retrieval-augmented agent already
      carries untrusted content on essentially every turn (GRAPH_PATTERNS.md
      pattern 12); once that's true, letting a mutating or outward-reaching
      tool run unsupervised too is exactly the "two of three legs" exposure
      this app has no reason to gamble on (see app/agent/tools.py::TOOL_CAPABILITIES
      for the full reasoning). An *undeclared* tool is treated the same as
      "outward," so forgetting to register a new tool's capability fails
      toward extra caution, not past it.

    `tool_capabilities`/`valid_tool_names` both default to the Ecorp domain's
    mapping/tool set so every existing test/caller invoking
    `should_continue(state)` directly is unaffected; `build_graph` binds a
    domain's own values for both via `functools.partial` before registering
    this as the `agent` node's conditional edge — see its docstring and
    GRAPH_PATTERNS.md pattern 23. `max_iterations`/`max_tokens`/`max_cost_usd`
    default to the same module constants the bare-global checks used before
    this signature grew these params — added so a NESTED subagent run
    (GRAPH_PATTERNS.md pattern 46) can be bound to its own, smaller
    MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN ceiling via the identical `functools.partial`
    mechanism, independent of the parent turn's own remaining budget.
    This stays a plain module-level function (not a factory, unlike
    `agent`/`retrieve_context`/the semantic-cache nodes) specifically so it
    remains directly importable and callable with just `state`, matching
    every other routing function in this file (see this module's own
    docstring on why routing functions live at module level).

    All four safety-net exits below route to `"no_answer"`, not straight to
    `END` — `no_answer_fallback` is the one place that turns "some budget
    fired before `check_output` ever ran" into a real, user-visible message
    instead of silently ending the turn on whatever the `agent` node's last
    AIMessage happened to be (often empty — a small model that fails to
    produce a tool call or any content still burns real tokens doing it, so
    a `retry_output` loop can hit `max_tokens` before ever producing prose).
    Same "empty final AIMessage means a safety net tripped" signal
    `run_subagent` (app/agent/tools.py) already uses for a NESTED run;
    this is the top-level-turn equivalent, which previously had none.
    """
    if state.get("iterations", 0) >= max_iterations:
        return "no_answer"
    # Folds any run_subagent spend THIS turn into the parent's own live
    # ceiling (GRAPH_PATTERNS.md pattern 46's disclosed gap) — each nested
    # run is still separately, independently bounded by its own
    # MAX_SUBAGENT_TOKENS_PER_RUN/MAX_SUBAGENT_COST_USD_PER_RUN; this only
    # makes the PARENT aware that delegating doesn't happen for free.
    subagent_spend = state.get("subagent_spend", [])
    effective_tokens = state.get("total_tokens", 0) + sum(t for t, _ in subagent_spend)
    if effective_tokens >= max_tokens:
        metrics.agent_token_budget_exceeded_total.inc()
        return "no_answer"
    effective_cost_usd = state.get("total_cost_usd", 0.0) + sum(c for _, c in subagent_spend)
    if effective_cost_usd >= max_cost_usd:
        # A HARD stop (GRAPH_PATTERNS.md pattern 35) — independent of the
        # token cap above: the same token count costs differently on
        # different model tiers, so a $ ceiling is not a derived quantity
        # of the token one, it's its own budget.
        metrics.agent_cost_ceiling_exceeded_total.inc()
        return "no_answer"
    result = tools_condition(state)  # type: ignore[arg-type]  # State is a valid Mapping at runtime; tools_condition's stub just doesn't say so
    if result != "tools":
        return "check_output"
    tool_calls = cast(AIMessage, state["messages"][-1]).tool_calls or []
    if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
        return "too_many_tool_calls"
    if _invalid_tool_call_names(tool_calls, valid_tool_names):
        return "invalid_tool_call"
    if _use_skill_called_without_search(tool_calls, state["messages"]):
        return "use_skill_without_search"
    if _consecutive_repeat_count(state["messages"]) >= MAX_REPEATED_ACTIONS:
        # Checked here, independently of MAX_ITERATIONS — a run looping
        # on one identical action would otherwise just exhaust the
        # iteration cap and settle spend indistinguishably from a run
        # that was actually converging (GRAPH_PATTERNS.md pattern 34).
        metrics.agent_no_progress_total.inc()
        return "no_answer"
    mandatory_reason = _mandatory_gate_reason(tool_calls, tool_capabilities)
    if mandatory_reason:
        metrics.agent_capability_gate_total.labels(capability=mandatory_reason).inc()
    if state.get("require_approval") or mandatory_reason:
        return "human_approval"
    return "tools"


# --- Node: check output — also extracts which offered citations the
# final answer actually used (see _used_citations) and how many cited
# markers were invented (see _ungrounded_claims_count). Recomputed from
# scratch every time this node runs, so a retry_output loop back to
# `agent` (a new answer, possibly citing different sources) doesn't leave
# stale values from the rejected short answer.
#
# `system_prompt` defaults to the module-level SYSTEM_PROMPT (the Ecorp
# domain's) so `graph.check_output(state)` stays directly callable exactly
# as every existing test already calls it — same "plain module-level
# function, not a factory" shape should_continue's own
# tool_capabilities/valid_tool_names defaults already use, for the
# identical reason (see should_continue's docstring). build_graph binds
# the CORRECT per-domain prompt via functools.partial, same mechanism as
# domain_should_continue. ---
def check_output(state: State, system_prompt: str = SYSTEM_PROMPT) -> dict:
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    citations = state.get("citations") or []

    # Strip a fabricated reference-list footer FIRST, before any
    # citation-related computation below — it isn't a real answer sentence
    # to judge grounding on either way, and cleaning it up front means
    # every field this node returns already reflects the text the user
    # will actually see.
    message_update: dict = {}
    cleaned_content = _strip_fabricated_reference_footer(content)
    if cleaned_content != content:
        metrics.agent_reference_footer_stripped_total.inc()
        content = cleaned_content
        message_update["messages"] = [last.model_copy(update={"content": content})]

    used = _used_citations(content, citations)
    if citations and not used and content:
        # Directional signal only (the opposite failure mode from
        # ungrounded_claims_count below) — not enforced. A legitimate
        # general-knowledge or calculator-only answer looks IDENTICAL to a
        # model that silently dropped a mandatory citation: retrieve_context
        # always returns its top-K docs regardless of actual relevance, and
        # the SYSTEM_PROMPT explicitly allows citing nothing for either of
        # those cases. route_after_check deliberately doesn't retry on
        # this, same reasoning as why it doesn't retry on a high
        # ungrounded_claims_count either — see this metric's own docstring.
        metrics.agent_zero_citations_total.inc()
    likely_misattributed = _likely_misattributed_citations(content, citations, used)
    if likely_misattributed:
        metrics.agent_misattributed_citations_total.inc()
    defers = _defers_instead_of_acting(content)
    if defers:
        metrics.agent_deferred_instead_of_acting_total.inc()
    fabricated = _fabricates_tool_output(content)
    if fabricated:
        metrics.agent_fabricated_tool_output_total.inc()
    skipped_tool = _skipped_required_sandbox_after_skill(state["messages"])
    if skipped_tool:
        metrics.agent_skipped_required_tool_total.inc()
    leaks_prompt = _leaks_system_prompt(content, system_prompt)
    if leaks_prompt:
        metrics.agent_system_prompt_leak_total.inc()

    # Auto-correct rather than retry: _insert_missing_citation_markers's
    # own docstring has the live evidence (7 different prompt-level
    # attempts, all failed) for why this fixes the marker directly instead
    # of routing to retry_output over it. `used`/`likely_uncited`/`content`
    # are all recomputed against the CORRECTED text below so every other
    # field this node returns (and _retry_reason's own inputs) reflect
    # what the user will actually see, not the pre-correction draft. Reuses
    # `message_update` from the reference-footer strip above, if that
    # already fired this round — both corrections compose onto the SAME
    # final message rather than each overwriting the other's fix.
    likely_uncited = _likely_uncited_citations(content, citations, used)
    if likely_uncited:
        corrected_content, fixed = _insert_missing_citation_markers(content, citations, used)
        if fixed:
            metrics.agent_citation_auto_inserted_total.inc()
            content = corrected_content
            used = _used_citations(content, citations)
            likely_uncited = _likely_uncited_citations(content, citations, used)
            message_update["messages"] = [last.model_copy(update={"content": content})]

    reason = _retry_reason(
        content, leaks_prompt, fabricated, skipped_tool, defers, likely_uncited, likely_misattributed
    )
    prior_reason = state.get("last_retry_reason")
    if reason is None:
        repeat_count = 0
    elif reason == prior_reason:
        repeat_count = (state.get("retry_reason_repeat_count") or 0) + 1
    else:
        repeat_count = 1
    return {
        **message_update,
        "used_citations": used,
        "ungrounded_claims_count": _ungrounded_claims_count(content, citations),
        "likely_uncited_citations": likely_uncited,
        "likely_misattributed_citations": likely_misattributed,
        "deferred_instead_of_acting": defers,
        "fabricated_tool_output": fabricated,
        "skipped_required_tool": skipped_tool,
        "leaks_system_prompt": leaks_prompt,
        "last_retry_reason": reason,
        "retry_reason_repeat_count": repeat_count,
    }


def route_after_check(
    state: State,
) -> Literal["retry_output", "retry_exhausted", "suggest_followups"]:
    reason = state.get("last_retry_reason")
    if reason is None:
        return "suggest_followups"
    if (state.get("retry_reason_repeat_count") or 0) >= MAX_CONSECUTIVE_SAME_RETRY_REASON:
        # The SAME rejection reason fired on consecutive rounds — the model
        # isn't converging (see MAX_CONSECUTIVE_SAME_RETRY_REASON's own
        # docstring), so another retry_output round would just spend a
        # real LLM call for the same outcome we can already predict.
        metrics.agent_retry_exhausted_total.inc()
        return "retry_exhausted"
    return "retry_output"
