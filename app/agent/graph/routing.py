"""The two conditional-edge gate functions — `should_continue` (after `agent`: tool call, final
answer, or safety budget?) and `check_output` (is the final answer good enough to end on?) — plus
`route_after_check`, which routes on check_output's result.

Their helpers: `graph/loop_guards.py` (tool-call fingerprinting and repeat detection, the
mandatory-approval gate), `graph/tool_guards.py` (`batch_problem`, the malformed-batch check),
`graph/output_checks.py` (the registry `check_output` runs) with `graph/citations.py` and
`graph/output_guardrails.py` (the heuristics behind its checks).
"""
from collections.abc import Mapping
from typing import Literal, cast

from langchain_core.messages import AIMessage
from langgraph.prebuilt import tools_condition

from app.agent.graph.citations import (
    count_ungrounded_claims,
    find_likely_uncited,
    find_used_citations,
    insert_missing_citation_markers,
)
from app.agent.graph.loop_guards import (
    consecutive_repeat_count,
    mandatory_gate_reason,
)
from app.agent.graph.output_checks import CheckContext, run_checks
from app.agent.graph.output_guardrails import (
    strip_closing_offer,
    strip_fabricated_reference_footer,
)
from app.agent.graph.state import (
    MAX_CONSECUTIVE_SAME_RETRY_REASON,
    MAX_ITERATIONS,
    MAX_REPEATED_ACTIONS,
    MAX_TOKENS_PER_TURN,
    State,
)
from app.agent.graph.tool_guards import batch_problem
from app.core import metrics
from app.core.config import MAX_COST_USD_PER_TURN


# --- Edge fn: after agent, route to tools / output check / abort ---
def should_continue(
    state: State,
    tool_capabilities: Mapping[str, str],
    valid_tool_names: frozenset[str],
    max_iterations: int = MAX_ITERATIONS,
    max_tokens: int = MAX_TOKENS_PER_TURN,
    max_cost_usd: float = MAX_COST_USD_PER_TURN,
) -> Literal[
    "tools",
    "human_approval",
    "tool_batch_guard",
    "check_output",
    "no_answer",
]:
    """Did the LLM call a tool, give a final answer, or hit a safety budget?

    Checked in order: iteration/token/cost caps (turn-wide safety nets,
    checked first regardless of what the LLM said) -> is this a tool call
    -> a malformed batch (`batch_problem`: too many calls, a name that isn't
    one of this domain's tools, or `use_skill` before `skill_search`) ->
    human approval. The batch check runs BEFORE human_approval deliberately:
    none of those is a real tool call a human could meaningfully approve, so
    they all go to `tool_batch_guard`, which answers every call and loops back
    to `agent` for a self-correcting retry instead of surfacing garbage for review.

    Two independent reasons route to `human_approval`, only one optional:
    - `require_approval` on state — opt-in (default False), see
      `chat.py`'s `--hitl` mode.
    - Any pending tool_call whose declared capability (`tool_capabilities`,
      the calling domain's own mapping) isn't "read_only" — mandatory,
      never skippable via `require_approval=False`. An *undeclared* tool is
      treated as "outward," so forgetting to register a capability fails
      toward extra caution.

    `tool_capabilities`/`valid_tool_names` are the calling domain's own;
    `build_graph` binds them via `functools.partial` (pattern 23). There is no
    shared default: "a registered tool" and its tier only mean something
    relative to one domain.
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
    if batch_problem(tool_calls, state["messages"], valid_tool_names):
        return "tool_batch_guard"
    if consecutive_repeat_count(state["messages"]) >= MAX_REPEATED_ACTIONS:
        # Checked independently of MAX_ITERATIONS — a run looping on one
        # identical action would otherwise just exhaust the iteration cap,
        # indistinguishable from one that's actually converging (pattern 34).
        metrics.agent_no_progress_total.inc()
        return "no_answer"
    mandatory_reason = mandatory_gate_reason(tool_calls, tool_capabilities)
    if mandatory_reason:
        metrics.agent_capability_gate_total.labels(capability=mandatory_reason).inc()
    if state.get("require_approval") or mandatory_reason:
        return "human_approval"
    return "tools"


# --- Node: check output — also extracts which offered citations were
# actually used (find_used_citations) and how many cited markers were invented
# (count_ungrounded_claims). Recomputed from scratch every call, so a
# retry_output round doesn't leave stale values from the rejected answer.
#
# `system_prompt` is the calling domain's own, bound by build_graph via
# functools.partial, so `reveals_system_prompt` checks an answer against the prompt
# it was ACTUALLY seeded with. ---
def check_output(
    state: State,
    system_prompt: str,
    enforce_citations: bool = True,
    skill_required_tools: tuple[str, ...] = (),
) -> dict:
    """Judge the final answer: strip a fabricated reference footer, cut a closing offer off a
    complete answer, repair a missing citation marker, then run every check in
    `graph/output_checks.py`'s registry.

    Returns `used_citations`/`ungrounded_claims_count` for the answer as it will be shown,
    `output_findings` (every check that fired, `{code: detail}`), `last_retry_reason` (the first by
    priority, or None) and `retry_reason_repeat_count`; plus a corrected `messages` entry when the
    footer or a closing offer was stripped or a marker inserted.

    `system_prompt`, `enforce_citations` and `skill_required_tools` are the calling domain's, bound by
    `build_graph` via `functools.partial`. `enforce_citations=False` (a nested subagent run) turns the
    two citation checks off: no uncited or misattributed finding, and no `[n]` marker is ever
    inserted. A subagent is told NOT to use bracket markers (they would name its own sources, which
    mean nothing to the parent), so judging or "repairing" its answer by them contradicted its own
    prompt and put a stray `[1]` in the text handed back. Every other check still applies.
    `skill_required_tools` is the subset of `SKILL_REQUIRED_TOOL_MARKERS` this domain actually has."""
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    citations = state.get("citations") or []

    # Strip a fabricated reference-list footer FIRST, before any citation
    # computation — it isn't a real answer sentence to judge grounding on,
    # and every field below should reflect the cleaned text.
    message_update: dict = {}
    cleaned_content = strip_fabricated_reference_footer(content)
    if cleaned_content != content:
        metrics.agent_reference_footer_stripped_total.inc()
        content = cleaned_content
        message_update["messages"] = [last.model_copy(update={"content": content})]

    # A closing offer after a complete answer is cut, not sent back: asking the model to rewrite
    # it does not work (strip_closing_offer's docstring has the trace). `content` stays what the
    # model wrote, for the checks that must see all of it; `body` is what the rest is judged on.
    body = strip_closing_offer(content)
    if body != content:
        metrics.agent_closing_offer_stripped_total.inc()
        message_update["messages"] = [last.model_copy(update={"content": body})]

    used = find_used_citations(body, citations)
    if citations and not used and body:
        # Directional signal only, not enforced — a legitimate
        # general-knowledge/calculator-only answer looks IDENTICAL to a
        # silently dropped mandatory citation (retrieve_context always
        # returns its top-K docs regardless of relevance, and the prompt
        # allows citing nothing for either case). route_after_check
        # deliberately doesn't retry on this.
        metrics.agent_zero_citations_total.inc()

    # Auto-correct rather than retry: insert_missing_citation_markers's
    # docstring has the evidence (7 different prompt-level attempts, all
    # failed) for fixing the marker directly instead of routing to
    # retry_output. `shown`/`shown_used` are what the user will actually see, so every returned
    # field reflects that. Reuses `message_update` so both corrections (footer strip + marker
    # insert) compose onto the same final message.
    shown, shown_used = body, used
    if enforce_citations and find_likely_uncited(body, citations, used):
        corrected_content, fixed = insert_missing_citation_markers(body, citations, used)
        if fixed:
            metrics.agent_citation_auto_inserted_total.inc()
            shown = corrected_content
            shown_used = find_used_citations(shown, citations)
            message_update["messages"] = [last.model_copy(update={"content": shown})]

    findings = run_checks(
        CheckContext(
            content=content,
            body=body,
            messages=state["messages"],
            citations=citations,
            used=used,
            shown=shown,
            shown_used=shown_used,
            system_prompt=system_prompt,
            enforce_citations=enforce_citations,
            skill_required_tools=skill_required_tools,
        )
    )
    reason = next(iter(findings), None)
    prior_reason = state.get("last_retry_reason")
    if reason is None:
        repeat_count = 0
    elif reason == prior_reason:
        repeat_count = (state.get("retry_reason_repeat_count") or 0) + 1
    else:
        repeat_count = 1
    return {
        **message_update,
        "used_citations": shown_used,
        "ungrounded_claims_count": count_ungrounded_claims(shown, citations),
        "output_findings": findings,
        "last_retry_reason": reason,
        "retry_reason_repeat_count": repeat_count,
    }


def route_after_check(
    state: State,
) -> Literal["retry_output", "retry_exhausted", "finish"]:
    """Retry, give up, or `"finish"`: the answer stands, and `build_graph` fans out to whichever
    post-answer steps the graph has (follow-up suggestions, the cache write-through). Those do not
    depend on each other, so they run in parallel rather than one after the other."""
    reason = state.get("last_retry_reason")
    if reason is None:
        return "finish"
    if (state.get("retry_reason_repeat_count") or 0) >= MAX_CONSECUTIVE_SAME_RETRY_REASON:
        # The SAME rejection reason fired on consecutive rounds — the
        # model isn't converging (see MAX_CONSECUTIVE_SAME_RETRY_REASON),
        # so another round would just spend a call for a predictable outcome.
        metrics.agent_retry_exhausted_total.inc()
        return "retry_exhausted"
    return "retry_output"
