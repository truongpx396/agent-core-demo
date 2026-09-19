"""`make_retrieve_context_node` — fetches relevant docs (and this
principal's memories) before the agent reasons (GRAPH_PATTERNS.md pattern
20). Split out of `app/agent/graph.py` for file size (see graph_cache.py
for the sibling split).

`_default_search` stays in `graph.py` itself (imported here) because
`tests/conftest.py`'s autouse fixture monkeypatches `graph._default_search`
on every test run, and `_assemble_shared_graph_parts` reads it as a bare
name there.
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
        """Fetch relevant docs (and this principal's memories) before the
        agent reasons, plus citation records for each numbered source
        (pattern 20).

        Reliability: degrade, never fail the run. This is enrichment, not
        the agent's only path to this data — the LLM can still call the
        search_docs tool directly and get ToolNode's error recovery. A
        Qdrant/embedding blip costs a worse first guess, not the whole
        turn — contrast with `agent`, which has no fallback and gets
        AGENT_RETRY_POLICY instead.

        `search` is awaited directly (not `asyncio.to_thread`'d) since the
        rerank leg moved to a dedicated reranker HTTP container; the
        still-CPU-bound sparse-embedding leg gets its own narrower
        `asyncio.to_thread` inside `qdrant_store.hybrid_search`.
        """
        last_human = _last_human_message(state["messages"])
        # The turn's question is, right now, the last message in state.
        # Recording its index here (not re-deriving "last human message"
        # later, which would find a synthetic retry_output message after a
        # few loop rounds) lets agent() anchor history_summary/context to
        # a fixed position — see State.context_anchor_index.
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
