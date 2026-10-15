"""Guardrail cluster for rejecting a bad tool-call batch and looping back
to agent for a self-correcting retry: `too_many_tool_calls` (over the
per-turn cap), `invalid_tool_call` (hallucinated/non-existent tool name),
and shared machinery — `reject_tool_calls`, `current_turn_messages`,
`invalid_tool_call_names`. Split out of
`app/agent/graph/core.py` for file size.

`use_skill_without_search` — a third node with this same shape — lives in
`graph/skill_guards.py` instead (grouped with the rest of the skill-misuse
family) and imports `reject_tool_calls` from here.

`current_turn_messages`/`invalid_tool_call_names`
are also used by `graph/routing.py`, and `current_turn_messages` by
`app/agent/graph/core.py`'s `make_agent_node` (deferred import to avoid a cycle).
"""
from typing import cast

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph.messages import is_user_message
from app.agent.graph.state import MAX_TOOL_CALLS_PER_TURN, State
from app.core import metrics


def reject_tool_calls(tool_calls: list, reason: str) -> list[ToolMessage]:
    """Every pending tool_call needs a matching ToolMessage, or the next LLM
    call fails OpenAI's tool-response validation — shared by
    human_approval's rejection path and too_many_tool_calls below, which
    both need to abort a batch of tool calls without running them.

    Each rejection carries the tool's `name`, like a real result does:
    `pending_skill_required_tool` counts a required tool as dealt with only when a ToolMessage
    NAMES it, so a call the person rejected (or any batch aborted here) used to leave the tool
    "pending", and the agent kept being told to call what had just been declined."""
    return [ToolMessage(content=reason, tool_call_id=tc["id"], name=tc["name"]) for tc in tool_calls]


def current_turn_messages(messages: list) -> list:
    """Messages in the CURRENT turn only — from the most recent message the
    person sent (inclusive; `retry_output`'s feedback is a HumanMessage too but
    not a turn start, see `is_user_message`) onward, never spanning into a
    prior turn.
    Shared by graph/routing.py's `_current_turn_tool_call_batches`
    (pattern 34) and `skipped_required_sandbox_after_skill`, each
    filtering it differently afterward."""
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])),
        None,
    )
    return messages[last_human_index:] if last_human_index is not None else messages


def invalid_tool_call_names(tool_calls: list, valid_tool_names: frozenset[str]) -> list[str]:
    """Names in this batch that aren't a registered tool at all — stricter
    than `tool_capability`'s "outward" default (which still assumes a
    real, just undeclared, tool). Guards against the model emitting a
    malformed/hallucinated tool call (seen with Ollama's native
    tool-calling on small models, e.g. qwen2.5:3b). `valid_tool_names` is the
    calling domain's own tool set, bound by `build_graph`: there is no shared default,
    because "a registered tool" only means something relative to one domain."""
    return [tc["name"] for tc in tool_calls if tc["name"] not in valid_tool_names]

# --- Node: safety budget — abort a turn where the LLM asked for more tool
# calls at once than MAX_TOOL_CALLS_PER_TURN allows (e.g. a confused model
# fanning out into dozens of searches), instead of running all of them. ---
def too_many_tool_calls(state: State) -> dict:
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    metrics.agent_tool_budget_exceeded_total.inc()
    rejections = reject_tool_calls(
        tool_calls,
        f"Too many tool calls requested at once (limit: {MAX_TOOL_CALLS_PER_TURN}). "
        "Please make fewer, more targeted tool calls.",
    )
    return {"messages": rejections}


# --- Node: abort a batch containing a non-existent tool name, instead of
# dispatching it or surfacing it to a human at human_approval (who couldn't
# meaningfully approve/reject a name that doesn't exist). Same reject+retry
# shape as too_many_tool_calls. `valid_tool_names` must be the SAME set
# should_continue routed on (`build_graph` binds the domain's own via
# `functools.partial`, like `check_output`'s `system_prompt`): judged against
# Ecorp's list instead, a support domain's real `create_ticket` was named to
# the model as a tool that doesn't exist. ---
def invalid_tool_call(state: State, valid_tool_names: frozenset[str]) -> dict:
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    metrics.agent_invalid_tool_call_total.inc()
    bad_names = sorted(set(invalid_tool_call_names(tool_calls, valid_tool_names)))
    names_text = ", ".join(repr(n) for n in bad_names) if bad_names else "one of them"
    rejections = reject_tool_calls(
        tool_calls,
        f"I tried to call a tool that doesn't exist ({names_text}). Let me try again "
        "using only the tools actually available to me.",
    )
    return {"messages": rejections}
