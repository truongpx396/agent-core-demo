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
        self.upsert_batch_sizes: list[int] = []
        self.query_points_calls: list[str] = []

    def recreate_collection(self, collection_name, **kwargs):
        self.recreate_calls.append(collection_name)

    def upsert(self, collection_name, points):
        self.upsert_calls.append(collection_name)
        self.upsert_batch_sizes.append(len(points))

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

    def test_a_batch_within_the_limit_is_one_call(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        qdrant_store.upsert([point])
        assert client.upsert_batch_sizes == [1]

    def test_a_large_batch_is_split_to_stay_under_qdrants_request_size_limit(self, monkeypatch):
        """Live-verified, not a guess: one real large-PDF ingest built a
        5700-point single upsert whose serialized body (~94MB) blew past
        Qdrant's own 32MB request limit and lost the whole batch — see
        _MAX_POINTS_PER_UPSERT_BATCH's own comment for the repro."""
        client = _fake_client(monkeypatch)
        limit = qdrant_store._MAX_POINTS_PER_UPSERT_BATCH
        points = [
            qdrant_store.build_point(point_id=str(i), dense_vector=[0.1], payload={})
            for i in range(limit + 50)
        ]

        qdrant_store.upsert(points)

        assert client.upsert_batch_sizes == [limit, 50]
        assert client.upsert_calls == [COLLECTION, COLLECTION]


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


class _FakePoint(SimpleNamespace):
    """Stands in for qdrant_client's real `ScoredPoint` (a Pydantic model
    that permits attribute mutation — confirmed live) just enough to
    exercise hybrid_search's score-threading: `.id`, `.payload`, `.score`."""


class TestHybridSearchRerankScore:
    """Real bug, found live via Langfuse (trace ed435567): the model cited
    a real, in-range marker on content that source didn't actually support.
    Root cause traced to hybrid_search returning Qdrant's RRF fusion score
    on `.score` even when the cross-encoder reranker ran — the reranker's
    own relevance judgment was computed, used only to reorder, then
    discarded, so nothing downstream could ever tell "ranked highest of a
    bad batch" apart from "actually relevant"."""

    def _mock_embeddings(self, monkeypatch, scores):
        monkeypatch.setattr(embeddings, "embed_text", lambda text: [0.1, 0.2])
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))
        monkeypatch.setattr(embeddings, "rerank", lambda query, texts: scores)

    def _fake_points(self, n):
        return [
            _FakePoint(id=str(i), payload={"text": f"doc {i}"}, score=0.5)
            for i in range(n)
        ]

    def test_returned_points_carry_the_reranker_score_not_the_rrf_score(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = lambda collection_name, **kw: SimpleNamespace(points=points)
        self._mock_embeddings(monkeypatch, scores=[-2.0, 6.5])

        result = qdrant_store.hybrid_search("query")

        # Reordered highest-reranker-score first, and `.score` now holds
        # that real cross-encoder value instead of the original RRF 0.5.
        assert [p.score for p in result] == [6.5, -2.0]

    def test_min_score_drops_points_below_the_floor(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(3)
        client.query_points = lambda collection_name, **kw: SimpleNamespace(points=points)
        self._mock_embeddings(monkeypatch, scores=[-11.4, 6.7, -5.9])

        result = qdrant_store.hybrid_search("query", min_score=-8.0)

        assert [p.score for p in result] == [6.7, -5.9]

    def test_min_score_is_a_noop_when_rerank_is_skipped(self, monkeypatch):
        """`min_score` is only meaningful against the cross-encoder's raw
        logit scale — RRF fusion scores are rank-derived, not comparable to
        it, so a caller that skips reranking (app/agent/tools.py's
        _memory_hits) must never have results silently dropped by it."""
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = lambda collection_name, **kw: SimpleNamespace(points=points)
        self._mock_embeddings(monkeypatch, scores=[-99.0, -99.0])

        result = qdrant_store.hybrid_search("query", rerank_results=False, min_score=-8.0)

        assert len(result) == 2

    def test_min_score_is_a_noop_when_reranker_degrades(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = lambda collection_name, **kw: SimpleNamespace(points=points)
        monkeypatch.setattr(embeddings, "embed_text", lambda text: [0.1, 0.2])
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))

        def broken_rerank(query, texts):
            raise RuntimeError("reranker model unavailable")

        monkeypatch.setattr(embeddings, "rerank", broken_rerank)

        result = qdrant_store.hybrid_search("query", min_score=-8.0)

        assert len(result) == 2
