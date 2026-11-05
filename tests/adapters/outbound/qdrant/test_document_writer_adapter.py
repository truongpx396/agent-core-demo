"""`QdrantDocumentWriter` (`app/adapters/outbound/qdrant/qdrant_store.py`), the `DocumentWriter` port (`app/ports/outbound/retrieval.py`) the ingest pipeline reaches."""
from app.adapters.outbound.qdrant import qdrant_store
from app.ports.outbound.retrieval import DocumentPoint


async def test_the_writer_stores_each_point_by_its_id_with_its_vectors_and_payload(monkeypatch):
    written = []

    async def upsert(points):
        written.extend(points)

    monkeypatch.setattr(qdrant_store, "upsert", upsert)

    await qdrant_store.QdrantDocumentWriter().upsert(
        [
            DocumentPoint("00000000-0000-0000-0000-000000000001", [0.1, 0.2], {"tenant": "acme", "text": "t"}, ([3], [0.5])),
            DocumentPoint("00000000-0000-0000-0000-000000000002", [0.3, 0.4], {"tenant": "acme", "text": "u"}),
        ]
    )

    assert [p.id for p in written] == ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
    assert [p.payload["tenant"] for p in written] == ["acme", "acme"]
    assert written[0].vector == qdrant_store.build_point("x", [0.1, 0.2], {}, ([3], [0.5])).vector
    assert written[1].vector == qdrant_store.build_point("x", [0.3, 0.4], {}).vector
