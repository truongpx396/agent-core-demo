"""`should_continue`'s private helper cluster: tool-call fingerprinting
and per-turn repeat/loop detection (`_tool_call_fingerprint`,
`_current_turn_tool_call_batches`, `_consecutive_repeat_count`), the
mandatory human-approval capability gate (`_tool_capability`,
`_mandatory_gate_reason`), and the use_skill-without-search guardrail
(`_use_skill_called_without_search`). Split out of `graph_routing.py` for
file size (see sibling splits `graph_citations.py`/
`graph_output_guardrails.py`, the two private helper clusters behind
`check_output`); no behavior change.

These are also directly unit-tested by `tests/agent/test_routing.py`,
imported from here now rather than from `graph_routing`.
"""
import json
from collections.abc import Mapping

from langchain_core.messages import AIMessage

from app.agent.graph_tools import _current_turn_messages
from app.agent.tools import TOOL_CAPABILITIES


def _tool_capability(name: str, tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES) -> str:
    """A tool absent from `tool_capabilities` defaults to "outward" — fail
    closed, so a new tool without a capability entry is gated rather than
    trusted. The only place this default is applied; elsewhere just reads
    the mapping. Defaults to `app/agent/tools.py`'s TOOL_CAPABILITIES
    (Ecorp) so existing callers work unchanged; `build_graph` passes a
    domain's own mapping instead (GRAPH_PATTERNS.md pattern 23)."""
    return tool_capabilities.get(name, "outward")


def _tool_call_fingerprint(tool_calls: list) -> str:
    """A stable fingerprint for one batch of tool calls — same tool
    name(s) + same args, independent of call order, so a model repeating
    the identical action (not just a coincidentally similar one) is what
    gets detected. Sorted so a batch of [A, B] and [B, A] fingerprint
    identically."""
    normalized = sorted(
        (tc["name"], json.dumps(tc["args"], sort_keys=True)) for tc in tool_calls
    )
    return json.dumps(normalized)


def _current_turn_tool_call_batches(messages: list) -> list:
    """Tool-call batches from AIMessages within the CURRENT turn only, most
    recent first — never spanning a prior turn. A per-turn loop-progress
    check (GRAPH_PATTERNS.md pattern 34): calling search_docs last turn
    and again this turn isn't a repeat."""
    turn_messages = _current_turn_messages(messages)
    return [
        m.tool_calls
        for m in reversed(turn_messages)
        if isinstance(m, AIMessage) and m.tool_calls
    ]


def _consecutive_repeat_count(messages: list) -> int:
    """How many of the most recent consecutive tool-call batches (within
    this turn) share the current one's fingerprint — a pure function of
    `state["messages"]`, no extra State field needed to track it, since
    the message history already IS the record of what's been tried."""
    batches = _current_turn_tool_call_batches(messages)
    if not batches:
        return 0
    target = _tool_call_fingerprint(batches[0])
    count = 0
    for batch in batches:
        if _tool_call_fingerprint(batch) != target:
            break
        count += 1
    return count


def _mandatory_gate_reason(
    tool_calls: list, tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES
) -> str | None:
    """None if every call in this batch is read_only; otherwise the more
    severe capability present ("outward" — including any undeclared tool —
    over "mutating"), used only to label the metric in should_continue.
    Gating itself doesn't care which one — either forces human_approval."""
    capabilities = {_tool_capability(tc["name"], tool_capabilities) for tc in tool_calls}
    if "outward" in capabilities:
        return "outward"
    if "mutating" in capabilities:
        return "mutating"
    return None


def _use_skill_called_without_search(tool_calls: list, messages: list) -> bool:
    """True if this batch calls `use_skill` but `skill_search` was never
    called earlier in THIS turn — SYSTEM_PROMPT requires searching first so
    the model uses a skill's real name instead of guessing one.
    `use_skill`'s own "no skill found" response recovers from a wrong name,
    but nothing stopped inventing one and never searching at all.

    Real bug, found live (Langfuse `197ab4e1`, 2026-09-09): the model
    called `use_skill(name="build_production_ai_agents")` — a name with no
    basis in the catalog — for a question already answerable from
    retrieved context, then narrated the resulting failure straight into
    the user-facing answer.

    Checked over `_current_turn_tool_call_batches` (pattern 34) — a
    skill_search from an earlier turn doesn't license skipping it now.
    """
    if not any(tc["name"] == "use_skill" for tc in tool_calls):
        return False
    return not any(
        any(tc["name"] == "skill_search" for tc in batch)
        for batch in _current_turn_tool_call_batches(messages)
    )
