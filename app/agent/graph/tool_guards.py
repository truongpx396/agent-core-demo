"""Guardrail cluster for rejecting a bad tool-call batch and looping back
to agent for a self-correcting retry: `too_many_tool_calls` (over the
per-turn cap), `invalid_tool_call` (hallucinated/non-existent tool name),
and shared machinery — `_reject_tool_calls`, `_current_turn_messages`,
`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES`. Split out of
`app/agent/graph.py` for file size.

`use_skill_without_search` — a third node with this same shape — lives in
`graph_skills.py` instead (grouped with the rest of the skill-misuse
family) and imports `_reject_tool_calls` from here.

`_current_turn_messages`/`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES`
are also used by `graph_routing.py`, and `_current_turn_messages` by
`graph.py`'s `make_agent_node` (deferred import to avoid a cycle).
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
    """Messages in the CURRENT turn only — from the most recent
    HumanMessage (inclusive) onward, never spanning into a prior turn.
    Shared by graph_routing.py's `_current_turn_tool_call_batches`
    (pattern 34) and `_skipped_required_sandbox_after_skill`, each
    filtering it differently afterward."""
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    return messages[last_human_index:] if last_human_index is not None else messages


_DEFAULT_VALID_TOOL_NAMES = frozenset(t.name for t in TOOLS)


def _invalid_tool_call_names(
    tool_calls: list, valid_tool_names: frozenset[str] = _DEFAULT_VALID_TOOL_NAMES
) -> list[str]:
    """Names in this batch that aren't a registered tool at all — stricter
    than `_tool_capability`'s "outward" default (which still assumes a
    real, just undeclared, tool). Guards against the model emitting a
    malformed/hallucinated tool call (seen with Ollama's native
    tool-calling on small models, e.g. qwen2.5:3b). `valid_tool_names`
    defaults to app/agent/tools.py's TOOLS (Ecorp); `build_graph` passes a
    domain's own tool set instead."""
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


# --- Node: abort a batch containing a non-existent tool name, instead of
# dispatching it or surfacing it to a human at human_approval (who couldn't
# meaningfully approve/reject a name that doesn't exist). Same reject+retry
# shape as too_many_tool_calls. Uses the module-default `valid_tool_names`
# (Ecorp's TOOLS) just to NAME the offending tool(s) in the retry message —
# should_continue already routed here using the correct domain-bound set,
# so a custom domain still rejects correctly; only the cited name(s) could
# differ. ---
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
