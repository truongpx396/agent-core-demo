"""tests/live/memories.py against a real (in-process) Qdrant: no Docker, no model, so it runs in the fast suite.

`QdrantClient(":memory:")` is the real client with real filter and paging semantics, which matters here:
the helper must remove exactly the memories a test added and nothing else, and a fake that returned what
the test hoped would not prove that.
"""
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from tests.live import memories

COLLECTION = "docs"


@pytest.fixture
def client() -> QdrantClient:
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection(COLLECTION, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    return qdrant


def _add(client: QdrantClient, point_id: int, kind: str) -> None:
    client.upsert(COLLECTION, [PointStruct(id=point_id, vector=[1.0, 0.0], payload={"kind": kind, "text": f"{kind} {point_id}"})])


def _ids(client: QdrantClient) -> set[int | str]:
    points, _ = client.scroll(COLLECTION, limit=10_000, with_payload=False, with_vectors=False)
    return {p.id for p in points}


def test_it_removes_the_memories_added_since_the_snapshot_and_nothing_else(client):
    _add(client, 1, "document")
    _add(client, 2, "document")
    _add(client, 10, "memory")  # already there before the test: not this test's to remove
    before = memories.memory_ids(client, COLLECTION)

    _add(client, 11, "memory")  # what the test remembered
    _add(client, 3, "document")  # and a document it (or a seed) added: never a memory, never deleted

    assert memories.forget_memories_not_in(client, COLLECTION, before) == 1
    assert _ids(client) == {1, 2, 3, 10}


def test_memory_ids_sees_only_memories(client):
    _add(client, 1, "document")
    _add(client, 2, "skill")
    _add(client, 3, "memory")
    assert memories.memory_ids(client, COLLECTION) == {3}


def test_it_pages_through_more_memories_than_one_page_holds(client):
    count = memories._PAGE * 2 + 7
    client.upsert(
        COLLECTION,
        [PointStruct(id=i, vector=[1.0, 0.0], payload={"kind": "memory"}) for i in range(1, count + 1)],
    )
    assert len(memories.memory_ids(client, COLLECTION)) == count
    assert memories.forget_memories_not_in(client, COLLECTION, set()) == count
    assert memories.memory_ids(client, COLLECTION) == set()


def test_with_nothing_to_forget_it_removes_nothing(client):
    _add(client, 1, "document")
    _add(client, 2, "memory")
    assert memories.forget_memories_not_in(client, COLLECTION, {2}) == 0
    assert _ids(client) == {1, 2}


def test_string_ids_work_too(client):
    # Real memory points use a uuid-string id (_tool_call_point_id), so ids are not always ints.
    uuid_id = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    client.upsert(COLLECTION, [PointStruct(id=uuid_id, vector=[1.0, 0.0], payload={"kind": "memory"})])
    assert memories.forget_memories_not_in(client, COLLECTION, set()) == 1
    assert memories.memory_ids(client, COLLECTION) == set()
