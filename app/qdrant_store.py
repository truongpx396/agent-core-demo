"""Thin Qdrant helpers: collection lifecycle, upsert, and hybrid search.

## Hybrid retrieval (GRAPH_PATTERNS.md pattern 20)

Every point carries TWO named vectors — `dense` (semantic, via LiteLLM) and
`sparse` (lexical/BM25, via `app/embeddings.py`'s local fastembed model).
`hybrid_search` fetches both legs with `Prefetch` and fuses them
server-side with Qdrant's own RRF (`FusionQuery(fusion=Fusion.RRF)`) — no
manual rank-fusion arithmetic here, Qdrant's Query API does it in one
round trip. The tenant/owner pre-filter (`app/security.py`'s
`Policy.lower`) is applied inside EACH `Prefetch`, not just at the top
level — a filter Qdrant applies only after fusion would mean the fused
candidate set briefly included rows this principal may not see, which is
exactly the pre-filter-not-post-filter discipline this module's `search`
function already existed to enforce before hybrid search was added.

Two independent degradation layers, matching this app's established
"multi-layer, narrowly-scoped, recorded-not-smoothed-over" reliability
policy (GRAPH_PATTERNS.md pattern 10):
1. Sparse embedding unavailable (fastembed model failed to load/embed) →
   degrade to a dense-only query. Still correct, still tenant-scoped,
   just lower recall than hybrid.
2. Reranking unavailable (cross-encoder failed to load/score) → degrade
   to the RRF-fused order Qdrant already returned. Still a cited,
   correctly-ordered-enough answer — reranking improves precision at the
   top of the list, it isn't what makes the list correct.

Both are recorded via `agent_retrieval_degraded_total{stage=...}`
(`app/metrics.py`), never silently absorbed.
"""
import logging

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    Fusion,
    FusionQuery,
    HasIdCondition,
    MatchValue,
    PointStruct,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app import metrics
from app.config import COLLECTION, HYBRID_PREFETCH_LIMIT, QDRANT_URL, RERANK_TOP_K

logger = logging.getLogger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"


def get_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(dim: int) -> None:
    """(Re)create the collection with a named dense vector (cosine) and a
    named sparse vector (BM25). Recreating changes the schema — a
    collection built before hybrid search (a single unnamed dense vector)
    is incompatible and must be re-ingested (`make ingest`)."""
    client = get_client()
    client.recreate_collection(
        collection_name=COLLECTION,
        vectors_config={DENSE_VECTOR_NAME: VectorParams(size=dim, distance=Distance.COSINE)},
        sparse_vectors_config={SPARSE_VECTOR_NAME: SparseVectorParams()},
    )


def build_point(
    point_id,
    dense_vector: list[float],
    payload: dict,
    sparse_vector: tuple[list[int], list[float]] | None = None,
) -> PointStruct:
    """One point, both vector legs — the single place that assembles the
    hybrid vector shape, so app/ingest.py and app/tools.py's write paths
    (add_note, remember) can't drift from each other or from
    `ensure_collection`'s schema. `sparse_vector` is optional (a point
    missing the sparse leg just never surfaces via that leg's Prefetch —
    still findable dense-only) so a caller whose sparse embedding failed
    can still write a point rather than losing the write entirely."""
    vector: dict = {DENSE_VECTOR_NAME: dense_vector}
    if sparse_vector is not None:
        indices, values = sparse_vector
        vector[SPARSE_VECTOR_NAME] = SparseVector(indices=indices, values=values)
    return PointStruct(id=point_id, vector=vector, payload=payload)


def upsert(points: list[PointStruct]) -> None:
    get_client().upsert(collection_name=COLLECTION, points=points)


def _build_filter(
    topic: str | None, tenant_filter: Filter | None, doc_ids: list[str] | None = None
) -> Filter | None:
    must: list[FieldCondition | HasIdCondition] = []
    if topic:
        must.append(FieldCondition(key="topic", match=MatchValue(value=topic)))
    if tenant_filter is not None:
        must.extend(tenant_filter.must or [])
    if doc_ids:
        # ANDed onto whatever's already in `must` (tenant, topic) — this
        # can only NARROW the result set to a caller-chosen subset of
        # already-permitted points, never widen it past the tenant filter
        # above (app/security.py's Policy.lower is still applied, and
        # applied first in every real call site — see app/tools.py's
        # search_docs docstring for why the ordering matters).
        must.append(HasIdCondition(has_id=doc_ids))
    return Filter(must=must) if must else None


def hybrid_search(
    query_text: str,
    topic: str | None = None,
    k: int | None = None,
    tenant_filter: Filter | None = None,
    rerank_results: bool = True,
    doc_ids: list[str] | None = None,
):
    """Dense+sparse hybrid search, RRF-fused, cross-encoder reranked —
    degrading gracefully at each stage (see module docstring). Returns a
    list of scored points (each has a `.payload`), reranked-and-truncated
    to `k` (default `RERANK_TOP_K`) when reranking succeeds, or the
    RRF/dense order truncated to `k` when it doesn't.

    `doc_ids`, when given, narrows results to those specific Qdrant point
    ids — ANDed onto the tenant/topic filter, never a replacement for it.
    """
    from app import embeddings  # deferred: avoids importing fastembed at module load

    k = k or RERANK_TOP_K
    query_filter = _build_filter(topic, tenant_filter, doc_ids)
    dense_vector = embeddings.embed_text(query_text)

    try:
        sparse_indices, sparse_values = embeddings.embed_sparse(query_text)
        response = get_client().query_points(
            collection_name=COLLECTION,
            prefetch=[
                Prefetch(
                    query=dense_vector,
                    using=DENSE_VECTOR_NAME,
                    filter=query_filter,
                    limit=HYBRID_PREFETCH_LIMIT,
                ),
                Prefetch(
                    query=SparseVector(indices=sparse_indices, values=sparse_values),
                    using=SPARSE_VECTOR_NAME,
                    filter=query_filter,
                    limit=HYBRID_PREFETCH_LIMIT,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=HYBRID_PREFETCH_LIMIT,
        )
        points = response.points
    except Exception as exc:  # noqa: BLE001 - degrade to dense-only, never fail the search
        logger.warning(
            "sparse leg of hybrid search unavailable; degrading to dense-only",
            extra={"error_class": type(exc).__name__},
        )
        metrics.agent_retrieval_degraded_total.labels(stage="sparse").inc()
        response = get_client().query_points(
            collection_name=COLLECTION,
            query=dense_vector,
            using=DENSE_VECTOR_NAME,
            query_filter=query_filter,
            limit=HYBRID_PREFETCH_LIMIT,
        )
        points = response.points

    if not points:
        return []

    if not rerank_results:
        return points[:k]

    try:
        texts = [p.payload.get("text", "") for p in points]
        scores = embeddings.rerank(query_text, texts)
        order = sorted(range(len(points)), key=lambda i: scores[i], reverse=True)
        return [points[i] for i in order][:k]
    except Exception as exc:  # noqa: BLE001 - degrade to the fused order, never fail the search
        logger.warning(
            "reranker unavailable; degrading to RRF-fused order",
            extra={"error_class": type(exc).__name__},
        )
        metrics.agent_retrieval_degraded_total.labels(stage="rerank").inc()
        return points[:k]


def delete_by_filter(delete_filter: Filter) -> None:
    """Support function for the "a memory must be removable" requirement
    (see app/tools.py's remember/MemoryService note) — a scoped, auditable
    delete, e.g. every point with `owner == <principal>`.

    Deliberately NOT exposed as an agent-facing tool: an LLM deciding to
    delete a principal's memories on their behalf is a different, harder
    trust question (real user intent vs. a model's interpretation of a
    request) than the retrieval/write-gating this app already handles, and
    conflating the two would blur a boundary that should stay sharp. This
    is the mechanism a real data-subject-request or retention-sweep script
    would call directly — the removability guarantee exists; wiring it to
    an autonomous decision-maker is a deliberate non-goal here.
    """
    get_client().delete(
        collection_name=COLLECTION, points_selector=FilterSelector(filter=delete_filter)
    )
