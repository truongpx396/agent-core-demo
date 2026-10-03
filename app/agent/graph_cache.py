"""Semantic-cache node pair (GRAPH_PATTERNS.md pattern 22):
`make_check_semantic_cache_node`/`route_after_cache` (read side — a hit
short-circuits to a final answer, no LLM call, no retrieval) and
`make_write_semantic_cache_node` (write-through side, skipped on a hit).
Split out of `app/agent/graph.py` for file size (see graph_retrieval.py
for the sibling split).

`_default_cache_get`/`_default_cache_set` stay in `graph.py` itself
(imported here) because `tests/conftest.py` and `graph_build.py`
monkeypatch/read them as `graph.<name>` — moving them would break that.
"""
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import AIMessage

from app.agent.graph import State, _default_cache_get, _default_cache_set
from app.agent.graph_loop_guards import _tool_capability
from app.agent.graph_messages import _human_text, _last_human_message
from app.agent.graph_tools import _current_turn_messages
from app.agent.tools import TOOL_CAPABILITIES

if TYPE_CHECKING:
    from app.core.security import SecurityCtx


# --- Node: semantic cache lookup (GRAPH_PATTERNS.md pattern 22) ---
def make_check_semantic_cache_node(
    cache_get: Callable[
        ["SecurityCtx | None", str], Awaitable[tuple[str, list[dict]] | None]
    ] = _default_cache_get,
):
    """Factory, same rationale as make_retrieve_context_node: needs an
    injected client so tests can fake it (see tests/agent/test_nodes.py) instead
    of monkeypatching app.retrieval.semantic_cache directly.
    """

    async def check_semantic_cache(state: State) -> dict:
        """A hit short-circuits to a final AIMessage — no LLM call, no
        retrieval — the entire point of a semantic cache. `cache_hit` lets
        write_semantic_cache skip re-caching an answer already served
        from cache.

        A miss returns `{}` and routing continues to retrieve_context;
        `semantic_cache.get` already swallows its own failures (a real
        miss and a degraded lookup both return None), so no try/except
        is needed here.
        """
        last_human = _last_human_message(state["messages"])
        if last_human is None:
            return {}
        hit = await cache_get(state.get("ctx"), _human_text(last_human))
        if hit is None:
            return {}
        answer, citations = hit
        return {
            "messages": [AIMessage(content=answer)],
            "citations": citations,
            "cache_hit": True,
        }

    return check_semantic_cache


def route_after_cache(state: State) -> Literal["retrieve_context", "check_output"]:
    return "check_output" if state.get("cache_hit") else "retrieve_context"


def _turn_acted(state: State, tool_capabilities: Mapping[str, str]) -> bool:
    """True if the CURRENT turn called any tool that is not `read_only`. An
    undeclared tool counts as `outward` (`_tool_capability`'s fail-closed
    default). Only this turn: an earlier turn's write says nothing about this
    one's answer."""
    return any(
        _tool_capability(call["name"], tool_capabilities) != "read_only"
        for message in _current_turn_messages(state["messages"])
        for call in (getattr(message, "tool_calls", None) or [])
    )


# --- Node: semantic cache write-through (GRAPH_PATTERNS.md pattern 22) ---
def make_write_semantic_cache_node(
    cache_set: Callable[
        ["SecurityCtx | None", str, str, list[dict]], Awaitable[None]
    ] = _default_cache_set,
    tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES,
):
    """Factory, same rationale as make_check_semantic_cache_node.
    `tool_capabilities` is the calling domain's own tier map (`build_graph`
    passes it), used to tell a turn that merely answered from one that acted."""

    async def write_semantic_cache(state: State) -> dict:
        """Only reached once a turn is confirmed final (route_after_check's
        non-retry branch) — never caches an answer about to be retried.

        Skips the write when `cache_hit` is set: nothing new to learn from
        a cache-served turn, and re-writing would waste work on the FAST
        path. Only a genuine miss (a real agent turn) writes here.

        Also skips a turn that called a tool that is not `read_only`. A cached
        answer is replayed with no model call and no approval pause, so caching
        "Remembered." / "Ticket #123 created" turned the next near-identical
        request into that sentence with nothing done — and with every write
        otherwise gated behind a person's approval, an approval bypassed
        (the cache hit never reaches `human_approval`). An answer that came
        from reading is still worth caching; an answer that came from acting is
        not an answer to be repeated.
        """
        if state.get("cache_hit"):
            return {}
        if _turn_acted(state, tool_capabilities):
            return {}
        last_human = _last_human_message(state["messages"])
        last = state["messages"][-1]
        content = getattr(last, "content", "") or ""
        if last_human is None or not content:
            return {}
        await cache_set(
            state.get("ctx"), _human_text(last_human), content, state.get("used_citations") or []
        )
        return {}

    return write_semantic_cache
