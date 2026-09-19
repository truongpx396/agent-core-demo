"""`make_retrieve_context_node` — the multi-step "fetch relevant docs (and
this principal's memories) before the agent reasons" node (GRAPH_PATTERNS.md
pattern 20). Split out of `app/agent/graph.py` purely for file size — see
that module's own docstring, and `app/agent/graph_cache.py` for the
sibling split (the semantic-cache node pair). No behavior change from the
pre-split single-file version.

`_default_search` (the swappable, real-hybrid-search default this factory
falls back to) deliberately stayed IN `graph.py` rather than moving here —
`tests/conftest.py`'s autouse `mock_search_docs` fixture monkeypatches
`graph._default_search` on every single test run, and
`app/agent/graph.py`'s own `_assemble_shared_graph_parts` reads it as a
bare name (safe, since it lives in the SAME file) to build the production
`retrieve_context` node — so it needs to keep living on the
`app.agent.graph` module object itself, imported from there below.
"""
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.agent.graph import State, _default_search
from app.agent.graph_messages import (
    _human_text,
    _last_human_message,
    _previous_human_message,
    _retrieval_query,
)
from app.core import metrics

if TYPE_CHECKING:
    from app.core.security import SecurityCtx

logger = logging.getLogger(__name__)


# --- Node: enrich context (multi-step pattern) ---
def make_retrieve_context_node(
    search: Callable[
        [str, "SecurityCtx | None"], Awaitable[tuple[str, list[dict]]]
    ] = _default_search,
):
    """Factory, not a plain function, because retrieve_context needs a
    search client — same rationale as make_agent_node for `agent`. Tests
    inject a fake via `make_retrieve_context_node(fake)(state)` instead of
    monkeypatching a module global (see tests/agent/test_nodes.py).
    """

    async def retrieve_context(state: State) -> dict:
        """Fetch relevant docs (and this principal's memories) *before* the
        agent reasons — and the citation records backing each numbered
        source in that text (GRAPH_PATTERNS.md pattern 20).

        In production, you might fetch from a database, call an API, etc.

        Reliability policy: degrade, never fail the run. This is enrichment,
        not the agent's only path to the same data — the LLM can still call
        the search_docs *tool* directly and get ToolNode's handle_tool_errors
        recovery (see the "tools" node in build_graph). A Qdrant/embedding
        blip here should cost the agent a slightly worse first guess, not
        the whole turn — contrast with `agent`, where a failed LLM call has
        nothing to fall back to and gets a retry policy instead
        (AGENT_RETRY_POLICY).

        `search` is an async `Callable`, same as `check_semantic_cache`'s
        `cache_get`/`write_semantic_cache`'s `cache_set` (app/agent/graph_cache.py)
        — their real implementations (app/retrieval/semantic_cache.py) await
        a real `redis.asyncio.Redis` client directly now.
        `gather_context`'s hybrid search used to be genuinely CPU-bound
        end to end (local ONNX sparse-embedding AND cross-encoder rerank),
        which is why this used to `asyncio.to_thread` the whole thing —
        but the rerank leg moved to a dedicated reranker container (see
        app/retrieval/embeddings.py's `rerank`), a real HTTP call, so
        `search` is `await`ed directly here now; the still-CPU-bound sparse
        leg gets its own, narrower `asyncio.to_thread` inside
        `qdrant_store.hybrid_search` instead of this whole call needing one.
        """
        last_human = _last_human_message(state["messages"])
        # The turn's opening question is, right now, the last message in
        # state — nothing appends after it until `agent` runs. Recording
        # its index here (not re-deriving "the last human message" later,
        # which would find a SYNTHETIC retry_output message instead once
        # the turn's loop has run a few rounds) is what lets agent() anchor
        # history_summary/context to the SAME fixed position on every call
        # — see State.context_anchor_index's own docstring.
        anchor = len(state["messages"]) - 1
        if last_human is None:
            return {"context": "", "citations": [], "context_anchor_index": anchor}
        try:
            query = _retrieval_query(
                _human_text(last_human), _previous_human_message(state["messages"], anchor)
            )
            context, citations = await search(query, state.get("ctx"))
            return {"context": context, "citations": citations, "context_anchor_index": anchor}
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the turn
            logger.warning(
                "context retrieval failed; continuing without pre-fetched context",
                extra={"node": "retrieve_context", "error_class": type(exc).__name__},
            )
            metrics.agent_context_retrieval_degraded_total.inc()
            return {"context": "", "citations": [], "context_anchor_index": anchor}

    return retrieve_context
