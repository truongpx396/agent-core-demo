"""The tool-batch guard: one node that turns a batch of tool calls the model should not have
made into a self-correcting retry, instead of running or surfacing it.

Three problems, checked in this order by `batch_problem` (which `should_continue` routes on and
`tool_batch_guard` re-derives, so there is one definition of each):

- `too_many_tool_calls` — more calls in one response than MAX_TOOL_CALLS_PER_TURN (a confused
  model fanning out into dozens of searches).
- `invalid_tool_call` — a name that is not one of THIS domain's tools (a hallucinated or
  malformed call, seen with Ollama's native tool-calling on small models, e.g. qwen2.5:3b). A
  human at `human_approval` couldn't meaningfully approve a name that doesn't exist.
- `use_skill_without_search` — `use_skill` before `skill_search` this turn. SYSTEM_PROMPT
  requires searching first so the model uses a skill's real name; without it, the model guessed
  a name that doesn't exist and narrated the "not found" failure into the final answer (Langfuse
  `197ab4e1`, 2026-09-09).

All three answer every pending call with a `ToolMessage` (`reject_tool_calls`) and loop back to
`agent`. Each keeps its own counter in `app/core/metrics.py`.
"""
from typing import Literal, cast

from langchain_core.messages import AIMessage

from app.agent.graph.loop_guards import use_skill_called_without_search
from app.agent.graph.messages import reject_tool_calls
from app.agent.graph.state import MAX_TOOL_CALLS_PER_TURN, State
from app.core import metrics

BatchProblem = Literal["too_many_tool_calls", "invalid_tool_call", "use_skill_without_search"]


def invalid_tool_call_names(tool_calls: list, valid_tool_names: frozenset[str]) -> list[str]:
    """Names in this batch that aren't a registered tool at all — stricter than
    `tool_capability`'s "outward" default (which still assumes a real, just undeclared, tool).
    `valid_tool_names` is the calling domain's own tool set, bound by `build_graph`: there is no
    shared default, because "a registered tool" only means something relative to one domain."""
    return [tc["name"] for tc in tool_calls if tc["name"] not in valid_tool_names]


def batch_problem(
    tool_calls: list, messages: list, valid_tool_names: frozenset[str]
) -> BatchProblem | None:
    """The first problem with this batch, in the priority order above, or None."""
    if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
        return "too_many_tool_calls"
    if invalid_tool_call_names(tool_calls, valid_tool_names):
        return "invalid_tool_call"
    if use_skill_called_without_search(tool_calls, messages):
        return "use_skill_without_search"
    return None


def _rejection_reason(
    problem: BatchProblem, tool_calls: list, valid_tool_names: frozenset[str]
) -> str:
    if problem == "too_many_tool_calls":
        return (
            f"Too many tool calls requested at once (limit: {MAX_TOOL_CALLS_PER_TURN}). "
            "Please make fewer, more targeted tool calls."
        )
    if problem == "invalid_tool_call":
        bad_names = sorted(set(invalid_tool_call_names(tool_calls, valid_tool_names)))
        names_text = ", ".join(repr(n) for n in bad_names) if bad_names else "one of them"
        return (
            f"I tried to call a tool that doesn't exist ({names_text}). Let me try again "
            "using only the tools actually available to me."
        )
    return (
        "You called use_skill without calling skill_search first this turn. "
        "Call skill_search now to find the exact name of a matching skill — "
        "if nothing matches well, don't guess a name; just answer directly "
        "using what you already have."
    )


_COUNTERS = {
    "too_many_tool_calls": metrics.agent_tool_budget_exceeded_total,
    "invalid_tool_call": metrics.agent_invalid_tool_call_total,
    "use_skill_without_search": metrics.agent_use_skill_without_search_total,
}


def tool_batch_guard(state: State, valid_tool_names: frozenset[str]) -> dict:
    """Reject the pending batch without running it. `valid_tool_names` is bound per domain by
    `build_graph` (`functools.partial`), the same set `should_continue` routed with."""
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    problem = batch_problem(tool_calls, state["messages"], valid_tool_names)
    if problem is None:  # should_continue only routes here for a real problem
        return {}
    _COUNTERS[problem].inc()
    return {"messages": reject_tool_calls(tool_calls, _rejection_reason(problem, tool_calls, valid_tool_names))}
