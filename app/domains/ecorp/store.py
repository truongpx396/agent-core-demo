"""The Ecorp domain's data access, as the adapter of its two ports (`app/ports/ecorp.py`): the knowledge-base writes behind `add_note`/`remember`
(embed, then upsert one Qdrant point) and the employee directory read behind `query_employees` (SQL).

This module is bound at the composition root as `KnowledgeBase` and `EmployeeDirectory`: the module itself is the adapter, its functions looked up at call
time. It stamps what the caller passes (`tenant`, `owner`, `created_at`) onto the point and nothing else; which id a point gets, who owns it and what is
returned to the model are the use cases' (`usecases.py`).
"""
import asyncio
from datetime import datetime

from app.core import sql_store
from app.retrieval import qdrant_store
from app.retrieval.embeddings import embed_document, embed_sparse


async def sparse_vector_or_none(text: str) -> tuple[list[int], list[float]] | None:
    """Best-effort BM25 sparse vector for a write path: if the sparse model
    fails to load/embed, the write still succeeds dense-only
    (`qdrant_store.build_point` treats `sparse_vector=None` as dense-only)
    rather than blocking a write on a hybrid-search quality concern — same
    degrade-don't-block posture as hybrid_search's read side.

    `embed_sparse` is local ONNX/CPU compute (`app/retrieval/embeddings.py`),
    so it's dispatched via `asyncio.to_thread` here too, same as
    `qdrant_store.py::hybrid_search`'s read-side call — a bare sync call
    would otherwise block every OTHER turn sharing this process's event
    loop for the embed's duration, and (worse) a `arun_with_timeout`
    caller has no `await` point during a bare sync call for its
    `asyncio.wait_for` timeout to actually cancel."""
    try:
        return await asyncio.to_thread(embed_sparse, text)
    except Exception:  # noqa: BLE001 - degrade to dense-only, never block the write
        return None


async def add_note(point_id: str, title: str, content: str, topic: str, tenant: str) -> None:
    """Embed and upsert one tenant-wide document point."""
    text = f"{title}: {content}"
    point = qdrant_store.build_point(
        point_id=point_id,
        dense_vector=await embed_document(text),
        sparse_vector=await sparse_vector_or_none(text),
        payload={
            "text": text,
            "topic": topic,
            "title": title,
            "kind": "document",
            "tenant": tenant,
        },
    )
    await qdrant_store.upsert([point])


async def add_memory(point_id: str, content: str, tenant: str, owner: str, created_at: datetime) -> None:
    """Embed and upsert one memory point, owned by `owner` within `tenant`."""
    point = qdrant_store.build_point(
        point_id=point_id,
        dense_vector=await embed_document(content),
        sparse_vector=await sparse_vector_or_none(content),
        payload={
            "text": content,
            "kind": "memory",
            "tenant": tenant,
            "owner": owner,
            "created_at": created_at.isoformat(),
        },
    )
    await qdrant_store.upsert([point])


async def query_employees(tenant: str, department: str | None, name_contains: str | None, limit: int) -> list[dict]:
    return await sql_store.query_employees(tenant=tenant, department=department, name_contains=name_contains, limit=limit)
