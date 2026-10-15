"""The two conditional-edge gate functions — `should_continue` (after
`agent`: tool call, final answer, or safety budget?) and `check_output`
(is the final answer good enough to end on?) — plus `route_after_check`,
which routes on check_output's result. Split out of graph.py purely for
file size; `State` and the other routing functions stay there since
they're tied to nodes that stay there too. No behavior change.

Each gate's own helpers live in further sibling splits:
graph_loop_guards.py (should_continue's tool-call fingerprinting/repeat
detection, mandatory-approval gate, use_skill-without-search check) and
graph_citations.py/graph_output_guardrails.py (check_output's citation-
grounding checks and deferral/fabrication/prompt-leak heuristics).
should_continue/check_output/route_after_check themselves stay at this
original import path so every existing caller/test is unaffected.

A few helpers that LOOK like they belong here stayed in their own sibling
files instead (`_invalid_tool_call_names`, `_current_turn_messages`,
`_pending_skill_required_tool`, `_content_words`) — each is also called
directly by a node function still in graph.py, so moving them here would
force graph.py to import back from this module, a real circular import.
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
    _ungrounded_claims_count,
    _used_citations,
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

    Checked in order: iteration/token/cost caps (turn-wide safety nets,
    checked first regardless of what the LLM said) -> is this a tool call
    -> too many tool calls at once -> any invalid tool name
    (`_invalid_tool_call_names`) -> `use_skill` called without
    `skill_search` first (`_use_skill_called_without_search`) -> human
    approval. The invalid-name and use_skill checks run BEFORE
    human_approval deliberately: neither is a real tool call a human could
    meaningfully approve, so both loop back to `agent` for a
    self-correcting retry instead of surfacing garbage for review.

    Two independent reasons route to `human_approval`, only one optional:
    - `require_approval` on state — opt-in (default False), see
      `chat.py`'s `--hitl` mode.
    - Any pending tool_call whose declared capability (`tool_capabilities`,
      default `tools.py::TOOL_CAPABILITIES`) isn't "read_only" — mandatory,
      never skippable via `require_approval=False`. An *undeclared* tool is
      treated as "outward," so forgetting to register a capability fails
      toward extra caution.

    `tool_capabilities`/`valid_tool_names` default to the Ecorp domain's
    mapping so existing direct callers are unaffected; `build_graph` binds
    a domain's own values via `functools.partial` (pattern 23).
    `max_iterations`/`max_tokens`/`max_cost_usd` default to this module's
    own constants, added so a NESTED subagent run (pattern 46) can bind its
    own smaller MAX_SUBAGENT_* ceiling the same way, independent of the
    parent turn's remaining budget. Stays a plain module-level function
    (not a factory) so it's directly callable with just `state`, like every
    other routing function here.

    All four safety-net exits below route to `"no_answer"`, not `END` —
    `no_answer_fallback` turns "a budget fired before check_output ever
    ran" into a real user-visible message instead of ending on `agent`'s
    last AIMessage, often empty (a model that fails to produce a tool call
    or content still burns tokens doing it). Same "empty final AIMessage
    means a safety net tripped" signal `run_subagent` already uses for a
    nested run; this is the top-level equivalent.
    """
    if state.get("iterations", 0) >= max_iterations:
        return "no_answer"
    # Folds any run_subagent spend THIS turn into the parent's own live
    # ceiling (pattern 46's disclosed gap) — each nested run is still
    # separately bounded by its own MAX_SUBAGENT_* ceiling.
    subagent_spend = state.get("subagent_spend", [])
    effective_tokens = state.get("total_tokens", 0) + sum(t for t, _ in subagent_spend)
    if effective_tokens >= max_tokens:
        metrics.agent_token_budget_exceeded_total.inc()
        return "no_answer"
    effective_cost_usd = state.get("total_cost_usd", 0.0) + sum(c for _, c in subagent_spend)
    if effective_cost_usd >= max_cost_usd:
        # A HARD stop (pattern 35), independent of the token cap above —
        # the same token count costs differently across model tiers, so $
        # is its own budget, not a derived quantity of tokens.
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
        # Checked independently of MAX_ITERATIONS — a run looping on one
        # identical action would otherwise just exhaust the iteration cap,
        # indistinguishable from one that's actually converging (pattern 34).
        metrics.agent_no_progress_total.inc()
        return "no_answer"
    mandatory_reason = _mandatory_gate_reason(tool_calls, tool_capabilities)
    if mandatory_reason:
        metrics.agent_capability_gate_total.labels(capability=mandatory_reason).inc()
    if state.get("require_approval") or mandatory_reason:
        return "human_approval"
    return "tools"


# --- Node: check output — also extracts which offered citations were
# actually used (_used_citations) and how many cited markers were invented
# (_ungrounded_claims_count). Recomputed from scratch every call, so a
# retry_output round doesn't leave stale values from the rejected answer.
#
# `system_prompt` defaults to module-level SYSTEM_PROMPT (Ecorp's) so
# `graph.check_output(state)` stays directly callable as tests already
# call it — same shape as should_continue's own defaults. build_graph
# binds the correct per-domain prompt via functools.partial. ---
def check_output(state: State, system_prompt: str = SYSTEM_PROMPT) -> dict:
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    citations = state.get("citations") or []

    # Strip a fabricated reference-list footer FIRST, before any citation
    # computation — it isn't a real answer sentence to judge grounding on,
    # and every field below should reflect the cleaned text.
    message_update: dict = {}
    cleaned_content = _strip_fabricated_reference_footer(content)
    if cleaned_content != content:
        metrics.agent_reference_footer_stripped_total.inc()
        content = cleaned_content
        message_update["messages"] = [last.model_copy(update={"content": content})]

    used = _used_citations(content, citations)
    if citations and not used and content:
        # Directional signal only, not enforced — a legitimate
        # general-knowledge/calculator-only answer looks IDENTICAL to a
        # silently dropped mandatory citation (retrieve_context always
        # returns its top-K docs regardless of relevance, and the prompt
        # allows citing nothing for either case). route_after_check
        # deliberately doesn't retry on this.
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
    # docstring has the evidence (7 different prompt-level attempts, all
    # failed) for fixing the marker directly instead of routing to
    # retry_output. `used`/`likely_uncited`/`content` are recomputed
    # against the CORRECTED text so every returned field reflects what the
    # user actually sees. Reuses `message_update` so both corrections
    # (footer strip + marker insert) compose onto the same final message.
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
        # The SAME rejection reason fired on consecutive rounds — the
        # model isn't converging (see MAX_CONSECUTIVE_SAME_RETRY_REASON),
        # so another round would just spend a call for a predictable outcome.
        metrics.agent_retry_exhausted_total.inc()
        return "retry_exhausted"
    return "retry_output"
