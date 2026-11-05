"""The readiness report reaches Qdrant through `DocumentIndex` (`app/ports/outbound/retrieval.py`), not the store module: `ping` and `identity_status`."""
import pytest

from app.adapters.outbound.qdrant import qdrant_store
from app.ports.outbound.retrieval import IdentityStatus


class _Client:
    def __init__(self, fails=False):
        self.fails = fails
        self.asked = 0

    async def get_collections(self):
        self.asked += 1
        if self.fails:
            raise ConnectionError("qdrant is down")


async def test_ping_asks_the_server_and_a_dead_one_raises(monkeypatch):
    client = _Client()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    index = qdrant_store.QdrantDocumentIndex()

    await index.ping()
    assert client.asked == 1

    monkeypatch.setattr(qdrant_store, "get_client", lambda: _Client(fails=True))
    with pytest.raises(ConnectionError):
        await index.ping()


async def test_identity_status_is_the_stores_observation_for_that_collection(monkeypatch):
    seen = []

    async def status(collection=None):
        seen.append(collection)
        return IdentityStatus("mismatch", "aaaaaaaa", "bbbbbbbb")

    monkeypatch.setattr(qdrant_store, "identity_status", status)

    result = await qdrant_store.QdrantDocumentIndex().identity_status("docs")

    assert (result.state, result.expected, result.found) == ("mismatch", "aaaaaaaa", "bbbbbbbb")
    assert seen == ["docs"]
