"""Thin Qdrant helpers: collection lifecycle, upsert, and hybrid search.

## Hybrid retrieval (GRAPH_PATTERNS.md pattern 20)

Every point carries TWO named vectors: `dense` (semantic, via LiteLLM) and `sparse` (lexical/BM25, local
fastembed). `hybrid_search` fetches both legs with `Prefetch` and fuses them server-side with Qdrant's RRF
(`FusionQuery(fusion=Fusion.RRF)`). The tenant/owner pre-filter (`Policy.lower`, app/core/security.py) is
applied inside EACH `Prefetch`, not only on top: filtering after fusion would let the fused candidates briefly
include rows the principal can't see.

Two independent degradation layers (pattern 10), both recorded as `agent_retrieval_degraded_total{stage=...}`:
sparse unavailable -> dense-only (still correct and scoped, lower recall); reranking unavailable -> the RRF
order (reranking sharpens the top, it doesn't make the list correct).

## Multiple collections, one schema (pattern 45)

`ensure_collection`/`upsert`/`hybrid_search` take an optional `collection` (default `COLLECTION`), so another
collection with the same dense+sparse schema (e.g. `SKILLS_COLLECTION`) gets the same fusion/rerank/degrade
pipeline.

## Embedding identity, and why every access goes through an alias (pattern 67)

A logical collection `docs` is the PHYSICAL collection `docs-1a2b3c4d` (the first 8 hex digits of the
embedding identity its vectors were made under, app/retrieval/embedding_identity.py) reached through the ALIAS
`docs-live`. Every read and write uses the alias and compares the suffix with the identity this process is
configured for:
- a mismatch is refused (`EmbeddingIdentityMismatch`): reads degrade like a retrieval outage, writes fail
  visibly. Mixing two embedding spaces returns confident nonsense that vector size can't catch;
- switching models builds `docs-<new hash>` beside the old one, verifies it, then repoints the alias in ONE
  atomic request, so the live collection never holds both and a failure before the repoint leaves the old one
  serving (`scripts/reembed.py`);
- a pre-existing collection named exactly `docs` with no alias is ADOPTED: `docs-live -> docs` is created
  (non-destructive) and the configured identity is trusted, with a warning and a metric, since nothing records
  what it was made with. The first re-embed replaces it with a verifiable one.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
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
from app.core.config import (
    COLLECTION,
    EMBED_IDENTITY_CACHE_SECONDS,
    HYBRID_PREFETCH_LIMIT,
    QDRANT_URL,
    RERANK_TOP_K,
)
from app.retrieval.embedding_identity import (
    HASH_LENGTH,
    EmbeddingIdentityMismatch,
    current_identity,
)

logger = logging.getLogger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"


def get_client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=QDRANT_URL)


LIVE_SUFFIX = "-live"
_HASH = re.compile(rf"^[0-9a-f]{{{HASH_LENGTH}}}$")


def live_name(collection: str | None = None) -> str:
    """The alias every read and write of a logical collection goes through."""
    return f"{collection or COLLECTION}{LIVE_SUFFIX}"


def physical_name(collection: str, identity_hash: str) -> str:
    return f"{collection}-{identity_hash}"


def identity_hash_of(physical: str, collection: str) -> str | None:
    """The identity hash a physical collection name carries, or None for a legacy collection (named exactly `collection`) or any name
    that does not follow the `<collection>-<8 hex>` shape."""
    prefix = f"{collection}-"
    if physical.startswith(prefix) and _HASH.match(physical[len(prefix) :]):
        return physical[len(prefix) :]
    return None


# logical collection -> (monotonic time the answer expires, the physical collection its alias points at, or None if nothing exists yet).
# Cached so a search costs no extra round trip; a re-embed's alias swap is picked up by every process within
# EMBED_IDENTITY_CACHE_SECONDS, and the process that performed it clears the entry (`reset_identity_cache`).
_targets: dict[str, tuple[float, str | None]] = {}


def reset_identity_cache() -> None:
    _targets.clear()


async def alias_target(collection: str | None = None) -> str | None:
    """The physical collection `<collection>-live` points at, or None if the alias does not exist."""
    wanted = live_name(collection)
    for alias in (await get_client().get_aliases()).aliases:
        if alias.alias_name == wanted:
            return alias.collection_name
    return None


async def ensure_live(collection: str | None = None) -> str | None:
    """The physical collection behind a logical one, adopting a legacy collection if that is all there is.

    Alias exists -> its target. No alias but a physical collection named exactly `collection` -> create `collection-live -> collection`
    (non-destructive, atomic) and return it: the deployment predates the alias, and its vectors are trusted to match the configured
    identity because nothing records otherwise (see the module docstring). Neither exists -> None (a fresh install before `make ingest`).
    Safe to call from every process at start: losing the race to create the alias is not an error.
    """
    logical = collection or COLLECTION
    target = await alias_target(logical)
    if target is not None:
        return target
    client = get_client()
    if not await client.collection_exists(logical):
        return None
    try:
        await client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(create_alias=CreateAlias(collection_name=logical, alias_name=live_name(logical)))
            ]
        )
        logger.warning(
            "adopted a pre-identity vector collection; its embedding identity cannot be verified until it is re-embedded",
            extra={"collection": logical},
        )
    except Exception:  # noqa: BLE001 - another process may have created the alias a moment ago; re-read before deciding it failed
        target = await alias_target(logical)
        if target is None:
            raise
        return target
    return logical


async def _resolve_target(logical: str) -> tuple[str | None, bool]:
    """(physical collection, whether this call refreshed the cached answer)."""
    now = time.monotonic()
    cached = _targets.get(logical)
    if cached is not None and cached[0] > now:
        return cached[1], False
    target = await ensure_live(logical)
    _targets[logical] = (now + EMBED_IDENTITY_CACHE_SECONDS, target)
    return target, True


async def check_identity(collection: str | None = None, *, write: bool) -> str:
    """The alias to use for `collection`, after proving its vectors match the embedding identity this process is configured for.

    Raises `EmbeddingIdentityMismatch` otherwise. A legacy (adopted) collection is trusted, not proven, and counted as such. Counted
    per refresh (verified / adopted / unverified) and per refused call (mismatch), so the alert on a mismatch keeps firing for as long
    as requests keep being refused.
    """
    logical = collection or COLLECTION
    target, refreshed = await _resolve_target(logical)
    alias = live_name(logical)
    if target is None:
        return alias  # nothing exists yet; the operation reports its own "collection not found"
    found = identity_hash_of(target, logical)
    if found is None:
        if refreshed:
            metrics.agent_embedding_identity_total.labels(outcome="adopted", collection=logical).inc()
        return alias
    identity = await current_identity()
    expected = identity.hash8()
    if expected != found:
        metrics.agent_embedding_identity_total.labels(outcome="mismatch", collection=logical).inc()
        raise EmbeddingIdentityMismatch(logical, expected, found, write=write)
    if refreshed:
        metrics.agent_embedding_identity_total.labels(outcome="verified" if identity.verified else "unverified", collection=logical).inc()
    return alias


@dataclass(frozen=True)
class IdentityStatus:
    state: str  # absent | adopted | mismatch | verified | unverified
    expected: str | None = None
    found: str | None = None


async def identity_status(collection: str | None = None) -> IdentityStatus:
    """Where a collection stands against the configured identity, for the readiness report. Unlike `check_identity` it never raises over a
    mismatch and counts nothing: it is an observation, not an access."""
    logical = collection or COLLECTION
    target, _ = await _resolve_target(logical)
    if target is None:
        return IdentityStatus("absent")
    found = identity_hash_of(target, logical)
    if found is None:
        return IdentityStatus("adopted")
    identity = await current_identity()
    expected = identity.hash8()
    if expected != found:
        return IdentityStatus("mismatch", expected, found)
    return IdentityStatus("verified" if identity.verified else "unverified", expected, found)


async def _maintenance_alias(collection: str | None) -> str:
    """The alias for an operation that must keep working on a mismatched collection (a data-subject delete, a count): resolved and
    adopted like any access, but never refused over the embedding identity, which is about vectors and these touch none."""
    logical = collection or COLLECTION
    await _resolve_target(logical)
    return live_name(logical)


def _collection_config() -> dict:
    return {
        "vectors_config": {DENSE_VECTOR_NAME: VectorParams(size=0, distance=Distance.COSINE)},
        "sparse_vectors_config": {
            # `Modifier.IDF` is required, not optional tuning: fastembed's `Qdrant/bm25` model only computes the term-frequency half of
            # BM25 locally; the IDF half is expected to come from Qdrant via this modifier, from the collection's indexed document
            # frequencies. Without it, common and rare terms score identically and the "BM25" leg isn't actually BM25.
            SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)
        },
    }


async def create_physical(physical: str, dim: int) -> None:
    """(Re)create one PHYSICAL collection with a named dense vector (cosine) and a named sparse vector (BM25). Destructive for a
    collection of that exact name, which is by construction one of the same embedding identity."""
    config = _collection_config()
    config["vectors_config"] = {DENSE_VECTOR_NAME: VectorParams(size=dim, distance=Distance.COSINE)}
    await get_client().recreate_collection(collection_name=physical, **config)


async def repoint_live(collection: str, physical: str) -> None:
    """Make `<collection>-live` point at `physical`, atomically: one request deletes the old alias (if any) and creates the new one, so
    no search ever sees a moment without it and none sees both collections."""
    ops: list = []
    if await alias_target(collection) is not None:
        ops.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=live_name(collection))))
    ops.append(CreateAliasOperation(create_alias=CreateAlias(collection_name=physical, alias_name=live_name(collection))))
    await get_client().update_collection_aliases(change_aliases_operations=ops)
    reset_identity_cache()


async def ensure_collection(dim: int, collection: str | None = None) -> None:
    """(Re)create the collection under the CURRENT embedding identity and make it the live one.

    Destructive only for a collection of the same identity (`make ingest` re-seeds from scratch by design); a collection built under
    another identity is left untouched and simply stops being live, so the old data survives until an operator deletes it.
    `collection` defaults to the main `COLLECTION` (docs) — pass e.g. `SKILLS_COLLECTION` to (re)create a SEPARATE collection with the
    same dense+sparse schema, as `scripts/index_skills.py` does."""
    logical = collection or COLLECTION
    physical = physical_name(logical, (await current_identity()).hash8())
    await create_physical(physical, dim)
    await repoint_live(logical, physical)


def build_point(
    point_id,
    dense_vector: list[float],
    payload: dict,
    sparse_vector: tuple[list[int], list[float]] | None = None,
) -> PointStruct:
    """One point, both vector legs — the single place that assembles the
    hybrid vector shape, so `scripts/seed.py` and `app/agent/tools/builtin.py`'s
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
    name = await check_identity(collection, write=True)
    client = get_client()
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
        # first — see app/agent/tools/builtin.py's search_docs docstring).
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
    """Dense+sparse hybrid search, RRF-fused and cross-encoder reranked, degrading at each stage (see module
    docstring). Returns scored points (each with `.payload`), reranked and truncated to `k` (default
    `RERANK_TOP_K`), or the RRF/dense order truncated to `k` when reranking is unavailable.

    `doc_ids` narrows results to those point ids, ANDed onto the tenant/topic filter and never replacing it.
    `collection` defaults to `COLLECTION`; pass e.g. `SKILLS_COLLECTION` for another collection with the same
    schema.

    `min_score` drops points whose cross-encoder score is below it: a real relevance floor, since RRF/dense
    order only says "most similar of what came back". It applies only when reranking ran, because only the
    cross-encoder's raw logit scale (unbounded, e.g. -11 to +6) is meaningful against it (RRF scores are
    rank-derived), so it is a no-op when `rerank_results=False` or reranking degrades.

    `async def`: `embed_query`/`rerank` (HTTP) and `query_points` (`AsyncQdrantClient`) are awaited directly;
    `embed_sparse` is local ONNX/CPU compute and runs via `asyncio.to_thread`.
    """
    # deferred: avoids importing fastembed at module load
    from app.retrieval import embeddings

    k = k or RERANK_TOP_K
    coll = await check_identity(collection, write=False)
    query_filter = _build_filter(topic, tenant_filter, doc_ids)
    dense_vector = await embeddings.embed_query(query_text)

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
        # reading `.score` (e.g. app/agent/tools/builtin.py's relevance floor) see
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
    (see app/agent/tools/builtin.py's remember/MemoryService note) — a scoped,
    auditable delete, e.g. every point with `owner == <principal>`.

    Deliberately NOT exposed as an agent-facing tool: an LLM deciding to
    delete a principal's memories is a harder trust question than the
    retrieval/write-gating this app handles, and blurs a boundary that
    should stay sharp. This is what a real data-subject-request or
    retention-sweep script would call directly.
    """
    await get_client().delete(
        collection_name=await _maintenance_alias(None), points_selector=FilterSelector(filter=delete_filter)
    )


async def count_by_filter(count_filter: Filter) -> int:
    """How many points currently match `count_filter` — used by
    `app/retrieval/memory.py::delete_memories` to report how many memories a
    deletion removed (Qdrant's `delete` doesn't return a row count, so
    this is called immediately BEFORE deleting the same filter — see that
    function's docstring for the accepted race)."""
    result = await get_client().count(collection_name=await _maintenance_alias(None), count_filter=count_filter)
    return result.count
