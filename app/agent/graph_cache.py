"""The semantic-cache node pair (GRAPH_PATTERNS.md pattern 22):
`make_check_semantic_cache_node`/`route_after_cache` (the read side — a
hit short-circuits straight to a final answer, no LLM call, no
retrieval) and `make_write_semantic_cache_node` (the write-through side,
skipped on a hit). Split out of `app/agent/graph.py` purely for file
size — see that module's own docstring, and `app/agent/graph_retrieval.py`
for the sibling split (`make_retrieve_context_node`). No behavior change
from the pre-split single-file version.

`_default_cache_get`/`_default_cache_set` (the swappable, real-Redis
defaults each factory falls back to) deliberately stayed IN `graph.py`
rather than moving here — `tests/conftest.py`'s autouse fixtures
monkeypatch `graph._default_cache_get`/`graph._default_cache_set` on
every single test run, and `app/agent/graph_build.py` reads them via
`graph_module._default_cache_get`/`.default_cache_set` for the identical
reason (see that file's own comment) — so they need to keep living on
the `app.agent.graph` module object itself, imported from there below.
"""
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import AIMessage

from app.agent.graph import State, _default_cache_get, _default_cache_set
from app.agent.graph_messages import _human_text, _last_human_message

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
        """A hit short-circuits straight to a final AIMessage — no LLM
        call, no retrieval — which is the entire latency point of a
        semantic cache. `cache_hit` is threaded through so
        write_semantic_cache can skip redundantly re-caching an answer
        that was already served from cache (see its docstring).

        A miss returns `{}` (no state change) and normal routing continues
        to retrieve_context — same "degrade to the ordinary path, never
        fail the turn" shape retrieve_context itself uses for a Qdrant
        outage; semantic_cache.get already swallows its own failures and
        returns None for both a real miss and a degraded lookup, so this
        node doesn't need its own try/except on top.

        `cache_get` is an async `Callable` — the real implementation
        (app/retrieval/semantic_cache.py) awaits a real `redis.asyncio.Redis`
        client directly now, so this just awaits it in place; tests inject
        async fakes (see tests/agent/test_nodes.py).
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


# --- Node: semantic cache write-through (GRAPH_PATTERNS.md pattern 22) ---
def make_write_semantic_cache_node(
    cache_set: Callable[
        ["SecurityCtx | None", str, str, list[dict]], Awaitable[None]
    ] = _default_cache_set,
):
    """Factory, same rationale as make_check_semantic_cache_node."""

    async def write_semantic_cache(state: State) -> dict:
        """Only reached once a turn is confirmed final (route_after_check's
        non-retry branch) — never caches a rejected-too-short answer that's
        about to be retried.

        Skips the write entirely when `cache_hit` is set: a turn served
        from cache has nothing new to learn — re-embedding the same query
        and re-writing the same answer back to Redis would just be wasted
        work on what's supposed to be the FAST path (see
        check_semantic_cache's docstring). Only a genuine miss — a real
        agent turn that ran retrieve_context + the LLM — writes here.

        `cache_set` is an async `Callable` — same reasoning as
        check_semantic_cache's `cache_get`.
        """
        if state.get("cache_hit"):
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
