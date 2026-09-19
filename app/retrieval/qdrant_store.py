"""Thin Qdrant helpers: collection lifecycle, upsert, and hybrid search.

## Hybrid retrieval (GRAPH_PATTERNS.md pattern 20)

Every point carries TWO named vectors — `dense` (semantic, via LiteLLM) and
`sparse` (lexical/BM25, local fastembed). `hybrid_search` fetches both
legs with `Prefetch` and fuses them server-side via Qdrant's own RRF
(`FusionQuery(fusion=Fusion.RRF)`). The tenant/owner pre-filter
(`app/core/security.py`'s `Policy.lower`) is applied inside EACH
`Prefetch`, not just at the top level — filtering only after fusion would
let the fused candidate set briefly include rows the principal can't see.

Two independent degradation layers (pattern 10's "narrowly-scoped,
recorded-not-smoothed-over" policy), both recorded via
`agent_retrieval_degraded_total{stage=...}`:
1. Sparse unavailable -> degrade to dense-only. Still correct/scoped, just
   lower recall.
2. Reranking unavailable -> degrade to the RRF-fused order. Reranking
   improves top-of-list precision; it isn't what makes the list correct.

## Multiple collections, one schema (GRAPH_PATTERNS.md pattern 45)

`ensure_collection`/`upsert`/`hybrid_search` all take an optional
`collection` (default `COLLECTION`) so a second collection with this same
dense+sparse schema — e.g. `SKILLS_COLLECTION` — gets the identical
fusion/rerank/degrade pipeline with no duplicated logic.
"""
import asyncio
import logging
from typing import cast
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    Fusion,
    FusionQuery,
    HasIdCondition,
    MatchValue,
    Modifier,
    PointStruct,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.core import metrics
from app.core.config import COLLECTION, HYBRID_PREFETCH_LIMIT, QDRANT_URL, RERANK_TOP_K

logger = logging.getLogger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"


def get_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL)


async def ensure_collection(dim: int, collection: str | None = None) -> None:
    """(Re)create the collection with a named dense vector (cosine) and a
    named sparse vector (BM25). Recreating changes the schema — a
    collection built before hybrid search (a single unnamed dense vector)
    is incompatible and must be re-ingested (`make ingest`).

    `collection` defaults to the main `COLLECTION` (docs) — pass e.g.
    `SKILLS_COLLECTION` to (re)create a SEPARATE collection with the same
    dense+sparse schema, as `scripts/index_skills.py` does."""
    # `Modifier.IDF` is required, not optional tuning: fastembed's
    # `Qdrant/bm25` model only computes the term-frequency half of BM25
    # locally; the IDF half is expected to come from Qdrant via this
    # modifier, from the collection's indexed document frequencies.
    # Without it, common and rare terms score identically and the "BM25"
    # leg isn't actually BM25.
    client = get_client()
    await client.recreate_collection(
        collection_name=collection or COLLECTION,
        vectors_config={DENSE_VECTOR_NAME: VectorParams(size=dim, distance=Distance.COSINE)},
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)
        },
    )


def build_point(
    point_id,
    dense_vector: list[float],
    payload: dict,
    sparse_vector: tuple[list[int], list[float]] | None = None,
) -> PointStruct:
    """One point, both vector legs — the single place that assembles the
    hybrid vector shape, so `scripts/seed.py` and `app/agent/tools.py`'s
    write paths (add_note, remember) can't drift from `ensure_collection`'s
    schema. `sparse_vector` is optional (a point missing it just never
    surfaces via that leg's Prefetch, still findable dense-only) so a
    failed sparse embedding doesn't lose the whole write."""
    vector: dict = {DENSE_VECTOR_NAME: dense_vector}
    if sparse_vector is not None:
        indices, values = sparse_vector
        vector[SPARSE_VECTOR_NAME] = SparseVector(indices=indices, values=values)
    return PointStruct(id=point_id, vector=vector, payload=payload)


# Qdrant rejects a request whose serialized JSON exceeds
# `service.max_request_size_mb` (32MiB, 400 error). A real 9.7MB PDF
# chunked into 5700 points serialized to ~94MB and was rejected
# ("JSON payload (94238282 bytes) is larger than allowed (limit:
# 33554432 bytes)"), losing the ~13min already spent embedding, since
# ingest_text upserts the whole points list in one call. 300/batch keeps
# each request well under the limit (worst-case point ~20KB -> ~6MB/batch,
# 5x headroom) without reasoning about chunk count per call site —
# add_note/remember (single point) and seed.py/index_skills.py (small
# corpora) never notice the batching.
_MAX_POINTS_PER_UPSERT_BATCH = 300


async def upsert(points: list[PointStruct], collection: str | None = None) -> None:
    client = get_client()
    name = collection or COLLECTION
    for start in range(0, len(points), _MAX_POINTS_PER_UPSERT_BATCH):
        batch = points[start : start + _MAX_POINTS_PER_UPSERT_BATCH]
        await client.upsert(collection_name=name, points=batch)


def _build_filter(
    topic: str | None, tenant_filter: Filter | None, doc_ids: list[str] | None = None
) -> Filter | None:
    must: list[Condition] = []
    if topic:
        must.append(FieldCondition(key="topic", match=MatchValue(value=topic)))
    if tenant_filter is not None:
        existing = tenant_filter.must
        must.extend(existing if isinstance(existing, list) else [existing] if existing else [])
    if doc_ids:
        # ANDed onto `must` (tenant, topic) — can only NARROW the result
        # set to a caller-chosen subset of already-permitted points, never
        # widen past the tenant filter (Policy.lower is still applied
        # first — see app/agent/tools.py's search_docs docstring).
        must.append(HasIdCondition(has_id=cast("list[int | str | UUID]", doc_ids)))
    return Filter(must=must) if must else None


async def hybrid_search(
    query_text: str,
    topic: str | None = None,
    k: int | None = None,
    tenant_filter: Filter | None = None,
    rerank_results: bool = True,
    doc_ids: list[str] | None = None,
    collection: str | None = None,
    min_score: float | None = None,
):
    """Dense+sparse hybrid search, RRF-fused, cross-encoder reranked —
    degrading gracefully at each stage (see module docstring). Returns
    scored points (each has `.payload`), reranked-and-truncated to `k`
    (default `RERANK_TOP_K`) when reranking succeeds, or the RRF/dense
    order truncated to `k` otherwise.

    `doc_ids`, when given, narrows results to those point ids — ANDed onto
    the tenant/topic filter, never a replacement for it.

    `collection` defaults to `COLLECTION` (docs) — pass e.g.
    `SKILLS_COLLECTION` to search a different collection with this same
    schema (see `ensure_collection`).

    `min_score`, when given, drops points whose cross-encoder score falls
    below it — a real relevance floor, not just a rank cutoff (RRF/dense
    order alone says "most similar of what came back," not "actually
    relevant"). Only applied when reranking actually ran: the cross-
    encoder's raw logit scale (unbounded, e.g. -11 vs +6) is the only scale
    it's meaningful against — RRF scores are rank-derived and not
    comparable, so this is a no-op when `rerank_results=False` or
    reranking degrades.

    `async def`: every leg is real I/O — `embed_text`/`rerank` (HTTP) and
    Qdrant's `query_points` (`AsyncQdrantClient`) are all awaited directly.
    `embed_sparse` is the exception — local ONNX/CPU compute, so it runs
    via `asyncio.to_thread` instead.
    """
    # deferred: avoids importing fastembed at module load
    from app.retrieval import embeddings

    k = k or RERANK_TOP_K
    coll = collection or COLLECTION
    query_filter = _build_filter(topic, tenant_filter, doc_ids)
    dense_vector = await embeddings.embed_text(query_text)

    try:
        sparse_indices, sparse_values = await asyncio.to_thread(
            embeddings.embed_sparse, query_text
        )
        response = await get_client().query_points(
            collection_name=coll,
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
        response = await get_client().query_points(
            collection_name=coll,
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
        texts = [(p.payload or {}).get("text", "") for p in points]
        scores = await embeddings.rerank(query_text, texts)
        # Overwrite the RRF fusion score (rank-derived, not a relevance
        # measure) with the cross-encoder's raw logit score, so callers
        # reading `.score` (e.g. app/agent/tools.py's relevance floor) see
        # an actual relevance judgment, not a fusion-rank artifact.
        for point, score in zip(points, scores, strict=True):
            point.score = score
        order = sorted(range(len(points)), key=lambda i: scores[i], reverse=True)
        if min_score is not None:
            order = [i for i in order if scores[i] >= min_score]
        return [points[i] for i in order][:k]
    except Exception as exc:  # noqa: BLE001 - degrade to the fused order, never fail the search
        logger.warning(
            "reranker unavailable; degrading to RRF-fused order",
            extra={"error_class": type(exc).__name__},
        )
        metrics.agent_retrieval_degraded_total.labels(stage="rerank").inc()
        return points[:k]


async def delete_by_filter(delete_filter: Filter) -> None:
    """Support function for the "a memory must be removable" requirement
    (see app/agent/tools.py's remember/MemoryService note) — a scoped,
    auditable delete, e.g. every point with `owner == <principal>`.

    Deliberately NOT exposed as an agent-facing tool: an LLM deciding to
    delete a principal's memories is a harder trust question than the
    retrieval/write-gating this app handles, and blurs a boundary that
    should stay sharp. This is what a real data-subject-request or
    retention-sweep script would call directly.
    """
    await get_client().delete(
        collection_name=COLLECTION, points_selector=FilterSelector(filter=delete_filter)
    )


async def count_by_filter(count_filter: Filter) -> int:
    """How many points currently match `count_filter` — used by
    `app/agent/memory.py::delete_memories` to report how many memories a
    deletion removed (Qdrant's `delete` doesn't return a row count, so
    this is called immediately BEFORE deleting the same filter — see that
    function's docstring for the accepted race)."""
    result = await get_client().count(collection_name=COLLECTION, count_filter=count_filter)
    return result.count
