"""Scoped, audited memory deletion — the removal half of cross-session
memory (GRAPH_PATTERNS.md pattern 33, extending pattern 18's "a memory
must be removable" note). `app/qdrant_store.py::delete_by_filter` is the
low-level mechanism; `delete_memories` here is the one function that
actually builds a correctly-scoped, correctly-audited selector on top of
it.

Deliberately NOT an agent-facing tool, same non-goal
`delete_by_filter`'s own docstring already states: an LLM deciding to
delete a principal's memories on their behalf is a harder trust question
(real user intent vs. a model's interpretation of a request) than the
retrieval/write gating this app already handles. This is the mechanism a
real data-subject-request handler or a retention-sweep script calls
directly — a trusted OPERATIONAL caller, never the graph.
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

from app import metrics, qdrant_store
from app.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)


def delete_memories(
    ctx: SecurityCtx,
    *,
    memory_id: str | None = None,
    older_than_days: int | None = None,
    target_principal: str | None = None,
) -> int:
    """Delete memories matching EXACTLY ONE selector — `memory_id` (a
    single memory) or `older_than_days` (every memory strictly older than
    N days). Never both, never neither: a selector this function can't
    honor in full is REFUSED (raises `ValueError`), not silently narrowed
    to "everything" or quietly ignored — the same "fail closed on an
    ambiguous request" discipline this app applies everywhere else.

    Always scoped to `ctx["tenant"]` — a deletion can never reach outside
    the caller's tenant, no matter what `target_principal` is asked for.
    `target_principal` defaults to `ctx["principal"]` (delete your own
    memories); passing a different principal lets a trusted operational
    caller (see module docstring) target another principal WITHIN THE
    SAME TENANT — e.g. a departed employee's data-subject-request. This
    module does not itself authenticate that the caller is entitled to
    target someone else — same trust boundary `app/ingest.py`'s
    script-identity ctx already relies on for its own operational writes.

    Returns the number of memories actually removed. Every call — refused
    or not — is recorded via `agent_memory_deletion_total{outcome=...}`
    (never carrying tenant/principal in a metric LABEL) plus a structured
    log line that DOES carry them (metadata, not a metric — same split
    `_instrumented`'s node-lifecycle logging already uses), so a deletion
    is never silent either way.
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
    # count-then-delete: Qdrant's delete call doesn't itself return a row
    # count, and the alternative (retrieve full points just to count them)
    # is strictly more expensive for the same answer. Accepts a narrow
    # race (a concurrent write between the count and the delete) as a
    # demo-scope tradeoff — a strictly exact count would need Qdrant to
    # return deleted ids directly, which its delete API doesn't offer.
    count = qdrant_store.count_by_filter(selector)
    qdrant_store.delete_by_filter(selector)

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
