"""`make_retrieve_context_node` — fetches relevant docs (and this
principal's memories) before the agent reasons (GRAPH_PATTERNS.md pattern
20).

The search callable is injected (`GraphDeps.search_docs`, defaulting to `graph/build.py`'s
`default_search`), so this module imports nothing from the composition root.
"""
import logging
from collections.abc import Awaitable, Callable

from app.agent.graph.messages import (
    human_text,
    last_human_message,
    previous_human_message,
    retrieval_query,
)
from app.agent.graph.state import State
from app.core import metrics
from app.core.security import SecurityCtx

logger = logging.getLogger(__name__)


# --- Node: enrich context (multi-step pattern) ---
def make_retrieve_context_node(
    search: Callable[[str, "SecurityCtx | None"], Awaitable[tuple[str, list[dict]]]],
):
    """Factory, not a plain function, because retrieve_context needs a
    search client — same rationale as make_agent_node for `agent`. Tests
    inject a fake via `make_retrieve_context_node(fake)(state)` instead of
    monkeypatching a module global (see tests/agent/graph/test_nodes.py).
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
        last_human = last_human_message(state["messages"])
        # The turn's question is, right now, the last message in state.
        # Recording its index here (not re-deriving "last human message"
        # later, which would find a synthetic retry_output message after a
        # few loop rounds) lets agent() anchor history_summary/context to
        # a fixed position — see State.context_anchor_index.
        anchor = len(state["messages"]) - 1
        if last_human is None:
            return {"context": "", "citations": [], "context_anchor_index": anchor}
        try:
            query = retrieval_query(
                human_text(last_human), previous_human_message(state["messages"], anchor)
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
