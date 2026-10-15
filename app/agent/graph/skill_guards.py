"""Skill-misuse guardrail family: `use_skill_without_search` (a node —
reject a batch calling `use_skill` without `skill_search` first this turn)
and `_pending_skill_required_tool`/`_SKILL_REQUIRED_TOOL_MARKERS` (a loaded
skill named a required tool, e.g. `run_python_in_sandbox`, that hasn't
been called yet this turn). Split out of `app/agent/graph.py` for file
size.

NOT `app/agent/skills.py` (the on-disk SKILL.md loader) — this is about
the agent misusing skill_search/use_skill mid-turn, not loading packages.

`use_skill_without_search` shares its "reject batch + retry" shape with
`too_many_tool_calls`/`invalid_tool_call` in `graph_tools.py`, importing
`_reject_tool_calls` from there.

`_pending_skill_required_tool` is called from `graph.py`'s `make_agent_node`
(proactive reminder, deferred import to avoid a cycle) and
`graph_routing.py`'s `_skipped_required_sandbox_after_skill` (reactive
check_output rejection).
"""
from typing import cast

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph import State
from app.agent.graph_tools import _reject_tool_calls
from app.core import metrics


# --- Node: reject a batch calling use_skill without skill_search first
# this turn, instead of letting the model guess a name that doesn't exist
# and narrate the "not found" failure into the final answer. Same
# reject+retry shape as invalid_tool_call (graph_tools.py). ---
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


# Tools a skill's body names as required (currently just
# run_python_in_sandbox, used by deal-economics/vendor-incident-postmortem/
# support-log-triage — added to avoid run_command_in_sandbox's
# shell-quoting failures). This is a crude substring match against skill
# text, not intent parsing: run_command_in_sandbox is deliberately excluded
# because skills used to mention it by name as a negative example ("not as
# a shell command"), which the substring check couldn't distinguish from a
# real requirement, causing false "you skipped this" corrections. Fixed at
# the source; kept out here too. A tuple, not a single name, so a future
# skill naming a different tool is picked up automatically — still an
# explicit allowlist, not a generic scanner (most skills need no tool).
_SKILL_REQUIRED_TOOL_MARKERS = ("run_python_in_sandbox",)


def _pending_skill_required_tool(turn_messages: list) -> str | None:
    """Name of a tool a skill loaded THIS TURN (via use_skill) named as
    required, if not yet called this turn — or None. `turn_messages` is
    already scoped to the current turn (see _current_turn_messages).
    Shared by `agent()` (proactive reminder before next generation) and
    `graph_routing.py::_skipped_required_sandbox_after_skill` (reactive
    check_output rejection). Returns the name, not a bool, so neither
    caller hardcodes which tool matched."""
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
