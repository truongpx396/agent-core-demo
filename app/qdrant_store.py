"""Thin Qdrant helpers: collection lifecycle, upsert, and filtered search.

`search`'s `tenant_filter` parameter is the pre-filter enforcement point for
app/security.py's Policy.lower() — it's AND-combined with `topic` inside the
same server-side query, never applied by filtering a Python list after the
fact. Post-filtering in Python would return the identical result for a
correct query and identical-*looking* wrong results for a buggy one — the
difference only shows up as a leak, which is exactly why the predicate has
to live here, in the query itself, and not one layer up in app/tools.py.
"""
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.config import COLLECTION, QDRANT_URL


def get_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(dim: int) -> None:
    """(Re)create the collection with the given vector size and cosine distance."""
    client = get_client()
    client.recreate_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )


def upsert(points: list[PointStruct]) -> None:
    get_client().upsert(collection_name=COLLECTION, points=points)


def search(
    vector: list[float],
    topic: str | None = None,
    k: int = 3,
    tenant_filter: Filter | None = None,
):
    """Vector search, optionally filtered to a single payload `topic`
    and/or a tenant/owner predicate from a Policy (`tenant_filter`).

    Both conditions are AND-combined into ONE query filter — a caller
    passing `tenant_filter` without `topic` (or vice versa) still gets a
    single server-side predicate, not two separate calls where the second
    one might be forgotten.

    Returns a list of scored points (each has a `.payload`).
    """
    must: list[FieldCondition] = []
    if topic:
        must.append(FieldCondition(key="topic", match=MatchValue(value=topic)))
    if tenant_filter is not None:
        must.extend(tenant_filter.must or [])
    query_filter = Filter(must=must) if must else None

    response = get_client().query_points(
        collection_name=COLLECTION,
        query=vector,
        query_filter=query_filter,
        limit=k,
    )
    return response.points


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
