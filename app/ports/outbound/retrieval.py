"""Retrieval: searching the knowledge base and caching answers, as the agent sees them.

`DocumentIndex` is hybrid search over tenant-scoped documents and per-person memories, plus lookups in the catalog collections (skills,
subagents) that hold no tenant data. The ranking pipeline (dense + sparse, fused, reranked, degrading at each stage) is the adapter's
(`app/adapters/outbound/qdrant/qdrant_store.py`); what the application owns is WHAT may be seen: every search of tenant data carries an `AccessScope`
(`app/core/security.py`), and the adapter MUST apply it as a pre-filter inside the query (constitution Principle I).

The two search methods are separate on purpose. `search` cannot be called without a scope, so a tenant-data search that forgets its scope is a
`TypeError`, not a cross-tenant read. `search_catalog` names the unscoped case, so every place that reads without a tenant is one grep away.

`AnswerCache` is the semantic cache of finished answers, keyed by the caller's tenant and person so one never serves another
(`app/adapters/outbound/redis/semantic_cache.py`). It fails open: a miss and an unreachable cache look the same to the graph.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.security import AccessScope, SecurityCtx
from app.ports.slot import Slot


@dataclass(frozen=True)
class SearchHit:
    """One result: the point's id, its relevance score (the reranker's raw logit when reranking ran, otherwise rank-derived) and its payload."""

    id: str | int
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


# How many texts one dense-embedding call should carry; a caller that wants progress between calls batches by this. The embedding endpoint rejects
# far more than this: 1000 per call broke the local Ollama backend and 700 hung past a minute, while 100 to 500 all worked, with no clear win past ~200.
EMBED_BATCH_SIZE = 200

SparseVector = tuple[list[int], list[float]]  # (indices, values) of a BM25 vector


@dataclass(frozen=True)
class DocumentPoint:
    """One chunk ready to store: its id, both vectors and its payload. The payload carries the tenant, which is how every search later scopes it."""

    point_id: str
    dense_vector: list[float]
    payload: dict[str, Any]
    sparse_vector: SparseVector | None = None


@dataclass(frozen=True)
class IdentityStatus:
    """Where a collection stands against the embedding identity this process is configured for (`absent | adopted | mismatch | verified | unverified`),
    with the two identities when they are known."""

    state: str
    expected: str | None = None
    found: str | None = None


class DocumentIndex(Protocol):
    async def search(
        self,
        query: str,
        *,
        scope: AccessScope,
        topic: str | None = None,
        k: int | None = None,
        rerank: bool = True,
        doc_ids: list[str] | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Hybrid search inside `scope`. `doc_ids` and `topic` only NARROW what the scope already permits. `min_score` is a relevance floor that
        applies only when reranking ran. Degrades (dense only, fused order) rather than failing when a stage is unavailable; raises when the
        collection itself cannot be searched."""
        ...

    async def search_catalog(self, query: str, *, collection: str, k: int | None = None) -> list[SearchHit]:
        """Search a catalog collection that holds no tenant data (the skills and subagents indexes). Never use it for tenant data."""
        ...

    async def ping(self) -> None:
        """Raises if the index's server is unreachable (the readiness probe)."""
        ...

    async def identity_status(self, collection: str | None = None) -> IdentityStatus:
        """An observation for the readiness report, not an access: it never raises over a mismatch and counts nothing."""
        ...


class Embedder(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Dense vectors for stored passages, in order. Send at most `EMBED_BATCH_SIZE` texts per call. Raises when the embedding endpoint fails."""
        ...

    def embed_sparse_batch(self, texts: list[str]) -> list[SparseVector]:
        """Sparse (BM25) vectors for `texts`, in order. Local CPU work: call it off the event loop. Raises on failure; the caller owns the degrade-to-dense-only policy."""
        ...


class DocumentWriter(Protocol):
    async def upsert(self, points: Sequence[DocumentPoint]) -> None:
        """Writes `points` to the live collection by id, so writing the same ids again overwrites instead of duplicating (what lets a retried ingest job repeat safely).
        Raises when the collection was built under another embedding identity."""
        ...


class AnswerCache(Protocol):
    async def get(self, ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict[str, Any]]] | None:
        """A cached `(answer, citations)` for a semantically close query from the same tenant and person, or None."""
        ...

    async def set(self, ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict[str, Any]]) -> None:
        """Remembers a finished answer. Best effort."""
        ...


document_index: Slot[DocumentIndex] = Slot("document_index", DocumentIndex)
embedder: Slot[Embedder] = Slot("embedder", Embedder)
document_writer: Slot[DocumentWriter] = Slot("document_writer", DocumentWriter)
answer_cache: Slot[AnswerCache] = Slot("answer_cache", AnswerCache)
