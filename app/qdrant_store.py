"""Thin Qdrant helpers: collection lifecycle, upsert, and filtered search."""
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
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


def search(vector: list[float], topic: str | None = None, k: int = 3):
    """Vector search, optionally filtered to a single payload `topic`."""
    query_filter = None
    if topic:
        query_filter = Filter(
            must=[FieldCondition(key="topic", match=MatchValue(value=topic))]
        )
    return get_client().search(
        collection_name=COLLECTION,
        query_vector=vector,
        query_filter=query_filter,
        limit=k,
    )
