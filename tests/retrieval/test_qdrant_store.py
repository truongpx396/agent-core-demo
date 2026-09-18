"""Tests for the `collection` parameter on
`app/retrieval/qdrant_store.py`'s `ensure_collection`/`upsert`/
`hybrid_search` (GRAPH_PATTERNS.md pattern 45) — the shared plumbing a
second collection (e.g. `SKILLS_COLLECTION`) reuses instead of forking the
dense+sparse+RRF+rerank+degrade pipeline. Every real Qdrant/embedding call
is mocked; this only proves the right `collection_name` reaches the client,
and that every existing call site (which omits `collection`) still targets
the original `COLLECTION` unchanged.

`hybrid_search` is `async def` now (it awaits `embeddings.rerank`, a real
HTTP call to the ml-service container — see that function's own
docstring), so every call below runs through `asyncio.run(...)`, this
repo's established pattern for exercising async code from a plain
`def test_...`. `embed_text`/`embed_sparse` mocks stay plain sync lambdas
(those two legs are unchanged); `rerank` mocks are small `async def`s.
"""
from types import SimpleNamespace

from qdrant_client.models import Modifier

from app.core.config import COLLECTION
from app.retrieval import embeddings, qdrant_store


class _FakeClient:
    def __init__(self):
        self.recreate_calls: list[str] = []
        self.recreate_kwargs: list[dict] = []
        self.upsert_calls: list[str] = []
        self.upsert_batch_sizes: list[int] = []
        self.query_points_calls: list[str] = []

    async def recreate_collection(self, collection_name, **kwargs):
        self.recreate_calls.append(collection_name)
        self.recreate_kwargs.append(kwargs)

    async def upsert(self, collection_name, points):
        self.upsert_calls.append(collection_name)
        self.upsert_batch_sizes.append(len(points))

    async def query_points(self, collection_name, **kwargs):
        self.query_points_calls.append(collection_name)
        return SimpleNamespace(points=[])


def _fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    return client


async def _async_embed_text(text):
    return [0.1, 0.2]


def _fixed_query_points(points):
    async def query_points(collection_name, **kwargs):
        return SimpleNamespace(points=points)

    return query_points


class TestEnsureCollection:
    async def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        await qdrant_store.ensure_collection(dim=4)
        assert client.recreate_calls == [COLLECTION]

    async def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        await qdrant_store.ensure_collection(dim=4, collection="skills")
        assert client.recreate_calls == ["skills"]

    async def test_sparse_vector_config_requests_idf_scoring(self, monkeypatch):
        """The fastembed `Qdrant/bm25` model (app/retrieval/embeddings.py)
        only computes the term-frequency half of BM25 locally and expects
        Qdrant to supply the IDF half via this modifier — omitting it
        silently downgrades the sparse leg to a plain TF dot product,
        no IDF weighting at all."""
        client = _fake_client(monkeypatch)
        await qdrant_store.ensure_collection(dim=4)
        sparse_config = client.recreate_kwargs[0]["sparse_vectors_config"]
        modifier = sparse_config[qdrant_store.SPARSE_VECTOR_NAME].modifier
        assert modifier == Modifier.IDF


class TestUpsert:
    async def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        await qdrant_store.upsert([point])
        assert client.upsert_calls == [COLLECTION]

    async def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        await qdrant_store.upsert([point], collection="skills")
        assert client.upsert_calls == ["skills"]

    async def test_a_batch_within_the_limit_is_one_call(self, monkeypatch):
        client = _fake_client(monkeypatch)
        point = qdrant_store.build_point(point_id="1", dense_vector=[0.1, 0.2], payload={})
        await qdrant_store.upsert([point])
        assert client.upsert_batch_sizes == [1]

    async def test_a_large_batch_is_split_to_stay_under_qdrants_request_size_limit(self, monkeypatch):
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

        await qdrant_store.upsert(points)

        assert client.upsert_batch_sizes == [limit, 50]
        assert client.upsert_calls == [COLLECTION, COLLECTION]


class TestHybridSearchCollection:
    def _mock_embeddings(self, monkeypatch):
        monkeypatch.setattr(embeddings, "embed_text", _async_embed_text)
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))

    async def test_defaults_to_the_main_collection(self, monkeypatch):
        client = _fake_client(monkeypatch)
        self._mock_embeddings(monkeypatch)
        await qdrant_store.hybrid_search("query")
        assert client.query_points_calls == [COLLECTION]

    async def test_targets_a_different_collection_when_given(self, monkeypatch):
        client = _fake_client(monkeypatch)
        self._mock_embeddings(monkeypatch)
        await qdrant_store.hybrid_search("query", collection="skills")
        assert client.query_points_calls == ["skills"]

    async def test_dense_only_degrade_path_also_respects_collection(self, monkeypatch):
        """The sparse-unavailable degrade branch (a SEPARATE query_points
        call) must target the same collection as the primary fused query —
        not silently fall back to the default COLLECTION."""
        client = _fake_client(monkeypatch)
        monkeypatch.setattr(embeddings, "embed_text", _async_embed_text)

        def broken_sparse(text):
            raise RuntimeError("sparse model unavailable")

        monkeypatch.setattr(embeddings, "embed_sparse", broken_sparse)

        await qdrant_store.hybrid_search("query", collection="skills")

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
        monkeypatch.setattr(embeddings, "embed_text", _async_embed_text)
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))

        async def fake_rerank(query, texts):
            return scores

        monkeypatch.setattr(embeddings, "rerank", fake_rerank)

    def _fake_points(self, n):
        return [
            _FakePoint(id=str(i), payload={"text": f"doc {i}"}, score=0.5)
            for i in range(n)
        ]

    async def test_returned_points_carry_the_reranker_score_not_the_rrf_score(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = _fixed_query_points(points)
        self._mock_embeddings(monkeypatch, scores=[-2.0, 6.5])

        result = await qdrant_store.hybrid_search("query")

        # Reordered highest-reranker-score first, and `.score` now holds
        # that real cross-encoder value instead of the original RRF 0.5.
        assert [p.score for p in result] == [6.5, -2.0]

    async def test_min_score_drops_points_below_the_floor(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(3)
        client.query_points = _fixed_query_points(points)
        self._mock_embeddings(monkeypatch, scores=[-11.4, 6.7, -5.9])

        result = await qdrant_store.hybrid_search("query", min_score=-8.0)

        assert [p.score for p in result] == [6.7, -5.9]

    async def test_min_score_is_a_noop_when_rerank_is_skipped(self, monkeypatch):
        """`min_score` is only meaningful against the cross-encoder's raw
        logit scale — RRF fusion scores are rank-derived, not comparable to
        it, so a caller that skips reranking (app/agent/tools.py's
        _memory_hits) must never have results silently dropped by it."""
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = _fixed_query_points(points)
        self._mock_embeddings(monkeypatch, scores=[-99.0, -99.0])

        result = await qdrant_store.hybrid_search("query", rerank_results=False, min_score=-8.0)

        assert len(result) == 2

    async def test_min_score_is_a_noop_when_reranker_degrades(self, monkeypatch):
        client = _fake_client(monkeypatch)
        points = self._fake_points(2)
        client.query_points = _fixed_query_points(points)
        monkeypatch.setattr(embeddings, "embed_text", _async_embed_text)
        monkeypatch.setattr(embeddings, "embed_sparse", lambda text: ([1], [0.5]))

        async def broken_rerank(query, texts):
            raise RuntimeError("reranker model unavailable")

        monkeypatch.setattr(embeddings, "rerank", broken_rerank)

        result = await qdrant_store.hybrid_search("query", min_score=-8.0)

        assert len(result) == 2
