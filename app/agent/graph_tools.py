"""The generic "reject a bad tool-call batch, loop back to agent for a
self-correcting retry" guardrail cluster: `too_many_tool_calls` (over the
per-turn cap), `invalid_tool_call` (a hallucinated/non-existent tool name),
and the machinery both share — `_reject_tool_calls`, `_current_turn_messages`,
`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES`. Split out of
`app/agent/graph.py` purely for file size — see that module's own docstring,
and `app/agent/graph_hitl.py`/`app/agent/graph_utils.py`/
`app/agent/graph_skills.py` for the sibling splits. No behavior change from
the pre-split single-file version.

`use_skill_without_search` — the THIRD node sharing this exact "reject +
retry" shape — deliberately lives in `app/agent/graph_skills.py` instead,
alongside the rest of the skill-misuse guardrail family
(`_pending_skill_required_tool`) it's thematically grouped with; it imports
`_reject_tool_calls` from here.

`_current_turn_messages`/`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES`
are also used by `app/agent/graph_routing.py` (imported from here, not
`app/agent/graph.py`) and, for `_current_turn_messages` specifically, by
`app/agent/graph.py`'s own `make_agent_node` — which imports it back via a
deferred (function-body-local) import to avoid a real circular import,
same pattern `_assemble_shared_graph_parts` already uses for
`check_output`/`should_continue`/`_make_llm`.
"""
from typing import cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph import MAX_TOOL_CALLS_PER_TURN, State
from app.agent.tools import TOOLS
from app.core import metrics


def _reject_tool_calls(tool_calls: list, reason: str) -> list[ToolMessage]:
    """Every pending tool_call needs a matching ToolMessage, or the next LLM
    call fails OpenAI's tool-response validation — shared by
    human_approval's rejection path and too_many_tool_calls below, which
    both need to abort a batch of tool calls without running them."""
    return [ToolMessage(content=reason, tool_call_id=tc["id"]) for tc in tool_calls]


def _current_turn_messages(messages: list) -> list:
    """All messages within the CURRENT turn only — after the most recent
    HumanMessage, inclusive — never spanning into a prior turn. Shared
    slice logic behind app/agent/graph_routing.py's own
    _current_turn_tool_call_batches (loop-progress, GRAPH_PATTERNS.md
    pattern 34) and _skipped_required_sandbox_after_skill
    (skill-instruction-compliance); both need "everything said and done so
    far in THIS turn," just filtered differently afterward."""
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    return messages[last_human_index:] if last_human_index is not None else messages


_DEFAULT_VALID_TOOL_NAMES = frozenset(t.name for t in TOOLS)


def _invalid_tool_call_names(
    tool_calls: list, valid_tool_names: frozenset[str] = _DEFAULT_VALID_TOOL_NAMES
) -> list[str]:
    """Names in this batch that aren't a real registered tool at all — a
    stricter, more severe check than `_tool_capability`'s "outward" default
    (which still assumes the name refers to a real tool, just an undeclared
    one). Guards against the model itself emitting a malformed/hallucinated
    tool call — e.g. Ollama's native tool-calling on a small model (verified
    against a real deployment: `qwen2.5:3b`, asked "list all tools available
    there," a query that shouldn't trigger any tool call) returning a
    garbled name for a query that shouldn't have triggered any tool call at
    all. Verified empirically that this app's own interrupt-payload/SSE-event
    plumbing (`human_approval` below, `app/agent/runtime.py`'s pass-through, the
    CLI/web UI's rendering) cannot itself produce a malformed name — every
    list construction and render path along that chain is structurally
    correct, so a bad name here can only be what the model already emitted.
    `valid_tool_names` defaults to app/agent/tools.py's TOOLS (the Ecorp domain) so
    every existing direct call keeps working unchanged; `build_graph` passes
    a domain's own tool set instead, same pattern as `tool_capabilities`
    above."""
    return [tc["name"] for tc in tool_calls if tc["name"] not in valid_tool_names]

# --- Node: safety budget — abort a turn where the LLM asked for more tool
# calls at once than MAX_TOOL_CALLS_PER_TURN allows (e.g. a confused model
# fanning out into dozens of searches), instead of running all of them. ---
def too_many_tool_calls(state: State) -> dict:
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    metrics.agent_tool_budget_exceeded_total.inc()
    rejections = _reject_tool_calls(
        tool_calls,
        f"Too many tool calls requested at once (limit: {MAX_TOOL_CALLS_PER_TURN}). "
        "Please make fewer, more targeted tool calls.",
    )
    return {"messages": rejections}


# --- Node: safety guardrail — abort a batch containing a tool name that
# isn't a real registered tool at all, instead of dispatching it or (worse)
# surfacing it to a human at human_approval, where nobody could meaningfully
# approve or reject a name that doesn't correspond to anything. Same
# "reject the whole batch + loop back to agent for a self-correcting retry"
# shape as too_many_tool_calls above — see _invalid_tool_call_names for why
# this exists (a small-model tool-calling fidelity issue, not a bug in this
# app's own control flow). Uses the module-default `valid_tool_names` (the
# Ecorp domain's TOOLS) to name the offending tool(s) in its message even
# for a non-default domain — should_continue already routed here using the
# CORRECT domain-bound set, so the whole batch is rejected regardless; this
# only affects which name(s), if any, get cited in the retry message for a
# custom domain whose tool set differs from Ecorp's. ---
def invalid_tool_call(state: State) -> dict:
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    metrics.agent_invalid_tool_call_total.inc()
    bad_names = sorted(set(_invalid_tool_call_names(tool_calls)))
    names_text = ", ".join(repr(n) for n in bad_names) if bad_names else "one of them"
    rejections = _reject_tool_calls(
        tool_calls,
        f"I tried to call a tool that doesn't exist ({names_text}). Let me try again "
        "using only the tools actually available to me.",
    )
    return {"messages": rejections}
