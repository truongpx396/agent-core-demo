"""Retrieval: searching the knowledge base and caching answers, as the agent sees them.

`DocumentIndex` is hybrid search over tenant-scoped documents and per-person memories, plus lookups in the catalog collections (skills,
subagents) that hold no tenant data. The ranking pipeline (dense + sparse, fused, reranked, degrading at each stage) is the adapter's
(`app/retrieval/qdrant_store.py`); what the application owns is WHAT may be seen: every search of tenant data carries an `AccessScope`
(`app/core/security.py`), and the adapter MUST apply it as a pre-filter inside the query (constitution Principle I).

The two search methods are separate on purpose. `search` cannot be called without a scope, so a tenant-data search that forgets its scope is a
`TypeError`, not a cross-tenant read. `search_catalog` names the unscoped case, so every place that reads without a tenant is one grep away.

`AnswerCache` is the semantic cache of finished answers, keyed by the caller's tenant and person so one never serves another
(`app/retrieval/semantic_cache.py`). It fails open: a miss and an unreachable cache look the same to the graph.
"""
from __future__ import annotations

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


class AnswerCache(Protocol):
    async def get(self, ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]] | None:
        """A cached `(answer, citations)` for a semantically close query from the same tenant and person, or None."""
        ...

    async def set(self, ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict]) -> None:
        """Remembers a finished answer. Best effort."""
        ...


document_index: Slot[DocumentIndex] = Slot("document_index", DocumentIndex)
answer_cache: Slot[AnswerCache] = Slot("answer_cache", AnswerCache)
