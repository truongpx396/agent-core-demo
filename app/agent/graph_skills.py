"""The skill-misuse guardrail family: `use_skill_without_search` (a node —
reject a batch that calls `use_skill` without `skill_search` first this
turn) and `_pending_skill_required_tool`/`_SKILL_REQUIRED_TOOL_MARKERS` (a
loaded skill named a tool as required for real computation, e.g.
`run_python_in_sandbox`, but the agent hasn't called it yet this turn).
Split out of `app/agent/graph.py` purely for file size — see that module's
own docstring, and `app/agent/graph_hitl.py`/`app/agent/graph_utils.py`/
`app/agent/graph_tools.py` for the sibling splits. No behavior change from
the pre-split single-file version.

NOT to be confused with `app/agent/skills.py` (the on-disk `SKILL.md`
package loader) — this file is about the agent MISUSING the skill_search/
use_skill tools mid-turn, not about loading skill packages from disk.

`use_skill_without_search` shares its "reject the whole batch + loop back
to agent for a self-correcting retry" shape with `too_many_tool_calls`/
`invalid_tool_call` in `app/agent/graph_tools.py`, so it imports
`_reject_tool_calls` from there rather than duplicating it.

`_pending_skill_required_tool` is called from TWO places that stay
elsewhere: `app/agent/graph.py`'s own `make_agent_node` (proactive — a
reminder injected before the next generation; imports this back via a
deferred, function-body-local import to avoid a real circular import, same
pattern `_assemble_shared_graph_parts` already uses for `check_output`/
`should_continue`/`_make_llm`) and `app/agent/graph_routing.py`'s
`_skipped_required_sandbox_after_skill` (reactive — a check_output
rejection after the fact, imported from here at that module's own top
level).
"""
from typing import cast

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph import State
from app.agent.graph_tools import _reject_tool_calls
from app.core import metrics


# --- Node: safety guardrail — abort a batch that calls use_skill without
# ever calling skill_search first this turn, instead of dispatching it and
# letting the model discover its own guessed name doesn't exist. Same
# "reject the whole batch + loop back to agent for a self-correcting
# retry" shape as invalid_tool_call above — see
# _use_skill_called_without_search for the real bug this exists for (a
# fabricated skill name, and its "not found" failure narrated straight
# into the final answer). ---
def use_skill_without_search(state: State) -> dict:
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    metrics.agent_use_skill_without_search_total.inc()
    rejections = _reject_tool_calls(
        tool_calls,
        "You called use_skill without calling skill_search first this turn. "
        "Call skill_search now to find the exact name of a matching skill — "
        "if nothing matches well, don't guess a name; just answer directly "
        "using what you already have.",
    )
    return {"messages": rejections}


# Every tool a current skill's own body names as required for real
# computation — deal-economics/vendor-incident-postmortem/support-log-triage
# all point at run_python_in_sandbox (added specifically to eliminate the
# shell-quoting failures run_command_in_sandbox kept hitting live — see
# that tool's own docstring, app/domains/{ops,support,sales}/tools.py).
# run_command_in_sandbox deliberately does NOT belong here: this is a
# crude SUBSTRING match against the skill's own text (see
# _pending_skill_required_tool below), not a real intent parse — a real
# bug, found live, was each skill's own "not as a shell command" aside
# mentioning run_command_in_sandbox BY NAME as a negative example, which
# the substring check can't tell apart from a genuine requirement, so it
# fired a false "you skipped this" correction on a turn that had ALREADY
# succeeded via run_python_in_sandbox. Fixed at the source (the skill
# text no longer names it at all) — kept out of this tuple too, so a
# future skill's own aside doesn't reintroduce the same false positive.
# Checked as a tuple, not a single hardcoded name, so a FUTURE skill
# naming a different required tool is picked up automatically instead of
# silently falling through this whole mechanism. Still not a fully
# generic "any tool a skill mentions" scanner — most skills genuinely
# don't require one at all (support-tier1-triage never mentions a tool),
# so this stays an explicit allowlist of tools worth nudging about, not a
# blind cross-reference against every tool name in the app.
_SKILL_REQUIRED_TOOL_MARKERS = ("run_python_in_sandbox",)


def _pending_skill_required_tool(turn_messages: list) -> str | None:
    """The name of a tool a skill loaded THIS TURN (via use_skill) named
    as required, if that tool hasn't been called yet anywhere in this
    turn — or None if no loaded skill named one of
    _SKILL_REQUIRED_TOOL_MARKERS, or the one it named was already called.
    `turn_messages` is already scoped to the current turn (see
    _current_turn_messages) — shared by TWO different callers checking
    the SAME facts at two different points for two different purposes:
    agent() (proactive — a recency-weighted reminder naming THIS specific
    tool, injected before the NEXT generation, same mechanism as the
    citation/history-summary reminders right above its own call site) and
    app/agent/graph_routing.py::_skipped_required_sandbox_after_skill
    (reactive — a check_output rejection AFTER the fact, if the proactive
    reminder didn't work). Returning the actual name (not just a bool) means
    neither caller has to hardcode which tool it's talking about — both
    read it from whichever marker actually matched."""
    for name in _SKILL_REQUIRED_TOOL_MARKERS:
        skill_named_it = any(
            isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "use_skill"
            and name in str(m.content)
            for m in turn_messages
        )
        if not skill_named_it:
            continue
        already_called = any(
            isinstance(m, ToolMessage) and getattr(m, "name", None) == name for m in turn_messages
        )
        if not already_called:
            return name
    return None
