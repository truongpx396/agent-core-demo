"""A real Qdrant round trip for app/retrieval/qdrant_store.py — the fake-free
counterpart to tests/retrieval/test_qdrant_store.py, which (correctly, for a
fast/hermetic suite) mocks `get_client()` entirely and only proves the right
`collection_name`/args reach a fake client. What that can't catch — a real
qdrant-client/qdrant-server wire-protocol mismatch (named dense+sparse
vectors, `FusionQuery(fusion=Fusion.RRF)`, `Prefetch`) breaking silently
across a dependency bump — needs an actual server, hence `@pytest.mark.integration`
(see tests/containers.py's own docstring for why this self-skips without
Docker rather than needing `make up`).

Uses the REAL `hybrid_search`/`ensure_collection`/`build_point`/`upsert` —
not hand-rolled vectors bypassing them — deliberately: those functions also
call the real, local `app.retrieval.embeddings` (fastembed dense+sparse+
rerank, no network, downloaded once and cached — see README's Prerequisites)
INTERNALLY, so this is the one test in this suite that exercises the exact
same code path `search_docs` runs in production, dense+sparse fusion and
reranking included, rather than a mock proving only that the right
arguments were passed.
"""
import pytest

from app.retrieval import embeddings, qdrant_store
from tests.containers import ensure_qdrant

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def real_qdrant(monkeypatch):
    info = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", info["qdrant_url"])


def test_hybrid_search_round_trips_through_a_real_server():
    collection = "integration-test-docs"
    dense_vector = embeddings.embed_text("checkpointers persist LangGraph state")
    qdrant_store.ensure_collection(dim=len(dense_vector), collection=collection)

    sparse_indices, sparse_values = embeddings.embed_sparse(
        "checkpointers persist LangGraph state"
    )
    point = qdrant_store.build_point(
        point_id=1,
        dense_vector=dense_vector,
        payload={"text": "A checkpointer persists LangGraph state across turns.", "tenant": "acme"},
        sparse_vector=(sparse_indices, sparse_values),
    )
    qdrant_store.upsert([point], collection=collection)

    results = qdrant_store.hybrid_search(
        "what persists state in LangGraph?", collection=collection
    )

    assert len(results) >= 1
    assert "checkpointer" in results[0].payload["text"].lower()


def test_a_point_with_no_sparse_vector_is_still_found_via_the_dense_leg():
    """`build_point(sparse_vector=None)` is real and supported — "a caller
    whose sparse embedding failed can still write a point" per its own
    docstring — so a point stored that way has NO entry in Qdrant's sparse
    index at all. That's not `hybrid_search`'s `except`-branch degrade (which
    fires when computing the QUERY's own sparse embedding fails, not when a
    STORED point lacks one); it's the ordinary two-leg RRF fusion, with this
    particular point contributed by only one leg. Still worth a real-server
    test: whether RRF fusion actually surfaces a point present in only ONE
    of its two `Prefetch` legs is real Qdrant server behavior, not something
    tests/retrieval/test_qdrant_store.py's mocked client (which stubs
    `query_points` to just return `[]` and asserts on the call args) can
    exercise."""
    collection = "integration-test-docs-dense-only"
    dense_vector = embeddings.embed_text("Acme support hours are 9am to 5pm weekdays")
    qdrant_store.ensure_collection(dim=len(dense_vector), collection=collection)

    point = qdrant_store.build_point(
        point_id=1,
        dense_vector=dense_vector,
        payload={"text": "Acme support hours are 9am to 5pm on weekdays.", "tenant": "acme"},
        sparse_vector=None,
    )
    qdrant_store.upsert([point], collection=collection)

    results = qdrant_store.hybrid_search("when is Acme support available?", collection=collection)

    assert len(results) >= 1
    assert "9am" in results[0].payload["text"]
