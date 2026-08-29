"""Tests for the `collection` parameter on
`app/retrieval/qdrant_store.py`'s `ensure_collection`/`upsert`/
`hybrid_search` (GRAPH_PATTERNS.md pattern 45) — the shared plumbing a
second collection (e.g. `SKILLS_COLLECTION`) reuses instead of forking the
dense+sparse+RRF+rerank+degrade pipeline. Every real Qdrant/embedding call
is mocked; this only proves the right `collection_name` reaches the client,
and that every existing call site (which omits `collection`) still targets
the original `COLLECTION` unchanged.
"""
from types import SimpleNamespace

from app.core.config import COLLECTION
from app.retrieval import embeddings, qdrant_store


class _FakeClient:
    def __init__(self):
        self.recreate_calls: list[str] = []
        self.upsert_calls: list[str] = []
        self.query_points_calls: list[str] = []

    def recreate_collection(self, collection_name, **kwargs):
        self.recreate_calls.append(collection_name)

    def upsert(self, collection_name, points):
        self.upsert_calls.append(collection_name)

    def query_points(self, collection_name, **kwargs):
        self.query_points_calls.append(collection_name)
        return SimpleNamespace(points=[])


def _fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    return client


class TestEnsureCollection:
    def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        qdrant_store.ensure_collection(dim=4)
        assert client.recreate_calls == [COLLECTION]

    def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        qdrant_store.ensure_collection(dim=4, collection="skills")
        assert client.recreate_calls == ["skills"]


class TestUpsert:
    def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        qdrant_store.upsert([point])
        assert client.upsert_calls == [COLLECTION]

    def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        qdrant_store.upsert([point], collection="skills")
        assert client.upsert_calls == ["skills"]


class TestHybridSearchCollection:
    def _mock_embeddings(self, monkeypatch):
        monkeypatch.setattr(embeddings, "embed_text", lambda text: [0.1, 0.2])
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))

    def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        self._mock_embeddings(monkeypatch)
        qdrant_store.hybrid_search("query")
        assert client.query_points_calls == [COLLECTION]

    def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        self._mock_embeddings(monkeypatch)
        qdrant_store.hybrid_search("query", collection="skills")
        assert client.query_points_calls == ["skills"]

    def test_dense_only_degrade_path_also_respects_collection(self, monkeypatch):
        """The sparse-unavailable degrade branch (a SEPARATE query_points
        call) must target the same collection as the primary fused query —
        not silently fall back to the default COLLECTION."""
        client = _fake_client(monkeypatch)
        monkeypatch.setattr(embeddings, "embed_text", lambda text: [0.1, 0.2])

        def broken_sparse(text):
            raise RuntimeError("sparse model unavailable")

        monkeypatch.setattr(embeddings, "embed_sparse", broken_sparse)

        qdrant_store.hybrid_search("query", collection="skills")

        assert client.query_points_calls == ["skills"]
