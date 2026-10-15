"""Scoped, audited memory deletion — the removal half of cross-session
memory (pattern 33, extending pattern 18's "a memory must be removable").
`qdrant_store.py::delete_by_filter` is the low-level mechanism;
`delete_memories` builds the correctly-scoped, correctly-audited selector
on top of it.

Deliberately NOT an agent-facing tool — an LLM deciding to delete a
principal's memories is a harder trust question than the retrieval/write
gating this app already does. Called directly by a trusted OPERATIONAL
caller (a data-subject-request handler, a retention sweep), never the
graph.
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from qdrant_client.models import (
    DatetimeRange,
    FieldCondition,
    Filter,
    HasIdCondition,
    MatchValue,
)

from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx
from app.retrieval import qdrant_store

logger = logging.getLogger(__name__)


async def delete_memories(
    ctx: SecurityCtx,
    *,
    memory_id: str | None = None,
    older_than_days: int | None = None,
    target_principal: str | None = None,
) -> int:
    """Delete memories matching EXACTLY ONE selector — `memory_id` (a
    single memory) or `older_than_days` (older than N days). Never both,
    never neither: an ambiguous selector is REFUSED (raises `ValueError`),
    never silently narrowed or ignored.

    Always scoped to `ctx["tenant"]`. `target_principal` defaults to
    `ctx["principal"]`; passing a different one lets a trusted operational
    caller target another principal WITHIN THE SAME TENANT (e.g. a
    departed employee's data-subject-request) — this module does not
    itself authenticate that entitlement.

    Returns the count removed. Every call (refused or not) is recorded via
    `agent_memory_deletion_total{outcome=...}` (no tenant/principal in the
    metric label) plus a structured log line that does carry them.
    """
    if not valid_ctx(ctx):
        metrics.agent_memory_deletion_total.labels(outcome="refused").inc()
        raise ValueError("a valid ctx is required to delete memories")
    if (memory_id is None) == (older_than_days is None):
        metrics.agent_memory_deletion_total.labels(outcome="refused").inc()
        logger.warning(
            "memory deletion refused: ambiguous selector",
            extra={"tenant": ctx["tenant"], "has_memory_id": memory_id is not None,
                   "has_older_than_days": older_than_days is not None},
        )
        raise ValueError(
            "delete_memories requires EXACTLY ONE of memory_id or older_than_days"
        )

    principal = target_principal or ctx["principal"]
    must: list = [
        FieldCondition(key="tenant", match=MatchValue(value=ctx["tenant"])),
        FieldCondition(key="kind", match=MatchValue(value="memory")),
        FieldCondition(key="owner", match=MatchValue(value=principal)),
    ]
    if memory_id is not None:
        must.append(HasIdCondition(has_id=[memory_id]))
    else:
        cutoff = datetime.now(UTC) - timedelta(days=cast(int, older_than_days))
        must.append(FieldCondition(key="created_at", range=DatetimeRange(lt=cutoff)))

    selector = Filter(must=must)
    # count-then-delete: Qdrant's delete call doesn't return a row count,
    # and retrieving full points just to count them is more expensive.
    # Accepts a narrow race (concurrent write between count and delete) as
    # a demo-scope tradeoff.
    count = await qdrant_store.count_by_filter(selector)
    await qdrant_store.delete_by_filter(selector)

    logger.info(
        "memory_deleted",
        extra={
            "tenant": ctx["tenant"],
            "principal": principal,
            "selector": "memory_id" if memory_id is not None else "older_than_days",
            "count": count,
        },
    )
    metrics.agent_memory_deletion_total.labels(outcome="deleted").inc()
    return count
