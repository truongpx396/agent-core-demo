"""`pending_skill_required_tool` / `SKILL_REQUIRED_TOOL_MARKERS`: a skill loaded this turn
(via `use_skill`) named a required tool, e.g. `run_python_in_sandbox`, that hasn't been called yet.

NOT `app/agent/tools/skill_catalog.py` (the on-disk SKILL.md loader) — this is about the agent
ignoring what a loaded skill told it to do mid-turn, not loading packages. (A skill used without
searching first is the tool-batch guard's concern: `graph/tool_guards.py`.)

`pending_skill_required_tool` is called from `graph/agent_node.py` (proactive reminder before the
next generation) and `graph/output_guardrails.py`'s `skipped_required_sandbox_after_skill`
(reactive `check_output` rejection).
"""
from langchain_core.messages import ToolMessage

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
SKILL_REQUIRED_TOOL_MARKERS = ("run_python_in_sandbox",)


def pending_skill_required_tool(turn_messages: list, required_tools: tuple[str, ...]) -> str | None:
    """Name of a tool a skill loaded THIS TURN (via use_skill) named as
    required, if not yet called this turn — or None. `turn_messages` is
    already scoped to the current turn (see current_turn_messages).
    `required_tools` is the subset of `SKILL_REQUIRED_TOOL_MARKERS` the calling
    domain actually has (`build_graph` computes it): a domain without the tool
    can never be told to call it, however a skill is worded.
    Shared by `agent()` (proactive reminder before next generation) and
    `graph/output_guardrails.py::skipped_required_sandbox_after_skill` (reactive
    check_output rejection). Returns the name, not a bool, so neither
    caller hardcodes which tool matched."""
    for name in required_tools:
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
