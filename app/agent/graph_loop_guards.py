"""`should_continue`'s own private helper cluster: tool-call fingerprinting
and per-turn repeat/loop detection (`_tool_call_fingerprint`,
`_current_turn_tool_call_batches`, `_consecutive_repeat_count`), the
mandatory human-approval capability gate (`_tool_capability`,
`_mandatory_gate_reason`), and the use_skill-without-search guardrail
check (`_use_skill_called_without_search`). Split out of
`app/agent/graph_routing.py` purely for file size — see that module's own
docstring, and `app/agent/graph_citations.py`/
`app/agent/graph_output_guardrails.py` for the sibling splits (the two
private helper clusters behind `check_output` instead). No behavior
change from the pre-split single-file version.

`_tool_capability`/`_consecutive_repeat_count`/`_mandatory_gate_reason`/
`_use_skill_called_without_search`/`_tool_call_fingerprint` are also
directly unit-tested by `tests/agent/test_routing.py`, imported from here
now rather than from `app.agent.graph_routing`.
"""
import json
from collections.abc import Mapping

from langchain_core.messages import AIMessage

from app.agent.graph_tools import _current_turn_messages
from app.agent.tools import TOOL_CAPABILITIES


def _tool_capability(name: str, tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES) -> str:
    """A tool absent from `tool_capabilities` defaults to "outward" — fail
    closed, so a new tool added to a domain's TOOLS without a capability
    entry is gated rather than silently trusted. This is the one place
    that default is applied; everywhere else just reads the mapping.
    `tool_capabilities` defaults to app/agent/tools.py's TOOL_CAPABILITIES (the
    Ecorp domain) so every existing direct call/import keeps working
    unchanged; `build_graph` passes a domain's own mapping instead (see
    its docstring and app/agent/manifest.py, GRAPH_PATTERNS.md pattern 23)."""
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
    """Tool-call batches from AIMessages within the CURRENT turn only, in
    reverse order (most recent first) — never spanning into a prior
    turn's tool calls. This is a per-turn loop-progress check
    (GRAPH_PATTERNS.md pattern 34), not a cross-conversation one: a model
    that called search_docs last turn and calls it again this turn hasn't
    repeated anything."""
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
    called earlier in THIS turn — SYSTEM_PROMPT requires skill_search
    first specifically so the model looks up a skill's real name instead
    of guessing one. `use_skill`'s own "no skill named X found" response
    (app/agent/tools.py) already recovers gracefully from a WRONG name,
    but nothing stopped the model from inventing one outright and never
    searching at all. Real bug, found live via Langfuse (trace
    `197ab4e1`, 2026-09-09): the model called
    `use_skill(name="build_production_ai_agents")` — a name with no basis
    in the actual catalog whatsoever (verified: no skill in this app's
    bundled catalog remotely resembles it) — for an ordinary "how do I
    build X" question that had nothing to do with any packaged skill and
    already had highly relevant retrieved context to answer from
    directly. It then narrated the resulting "no skill found" failure
    straight into the user-facing answer once use_skill returned it —
    an internal tool-naming miss leaking out as if it were part of the
    real answer.

    Checked over `_current_turn_tool_call_batches` (already-established
    per-turn helper, GRAPH_PATTERNS.md pattern 34) — a skill_search from
    an EARLIER turn doesn't license skipping it on a fresh question now.
    """
    if not any(tc["name"] == "use_skill" for tc in tool_calls):
        return False
    return not any(
        any(tc["name"] == "skill_search" for tc in batch)
        for batch in _current_turn_tool_call_batches(messages)
    )
