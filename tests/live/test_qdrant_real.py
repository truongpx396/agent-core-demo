"""A real Qdrant + real embedding-model round trip for
app/retrieval/qdrant_store.py — the fake-free counterpart to
tests/retrieval/test_qdrant_store.py, which (correctly, for a fast/hermetic
suite) mocks `get_client()` entirely and only proves the right
`collection_name`/args reach a fake client. What that can't catch — a real
qdrant-client/qdrant-server wire-protocol mismatch (named dense+sparse
vectors, `FusionQuery(fusion=Fusion.RRF)`, `Prefetch`) breaking silently
across a dependency bump — needs an actual server, hence `@pytest.mark.llm`
(see tests/containers.py's own docstring for why this self-skips without
Docker rather than needing `make up`).

`@pytest.mark.llm`, not `@pytest.mark.integration` — moved here from
tests/integration/ after a real CI run caught `openai.APIConnectionError`:
`app.retrieval.embeddings.embed_text` (dense leg) is a REAL network call to
an embedding API (`app/retrieval/embeddings.py`'s own docstring: "Dense
stays routed through LiteLLM like every other model call in this app"),
NOT the local fastembed model this file's own docstring originally, and
incorrectly, assumed it was — only `embed_sparse`/`rerank` are local. The
`test-integration` job deliberately provisions no LLM/embedding backend at
all (that's the whole reason it's cheap), so this needs `tests/live/`'s
`ollama_endpoint` fixture (a real `nomic-embed-text` model, sharing the same
container `test_agent_tool_calling.py`'s chat model uses — see
tests/containers.py::ensure_ollama) alongside `ensure_qdrant()`.

Uses the REAL `hybrid_search`/`ensure_collection`/`build_point`/`upsert` —
not hand-rolled vectors bypassing them — deliberately: this is the one test
in this suite that exercises the exact same code path `search_docs` runs in
production, dense+sparse fusion and reranking included, rather than a mock
proving only that the right arguments were passed.
"""
import pytest
from pydantic import SecretStr

from app.retrieval import embeddings, qdrant_store
from tests.containers import ensure_qdrant

pytestmark = pytest.mark.llm


@pytest.fixture(autouse=True)
def real_qdrant_and_embeddings(monkeypatch, ollama_endpoint):
    qdrant_info = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", qdrant_info["qdrant_url"])

    # `embeddings.embeddings` is a module-level `OpenAIEmbeddings` CLIENT
    # OBJECT, constructed once at import time (app/retrieval/embeddings.py) —
    # unlike CHAT_MODEL/OPENAI_API_BASE elsewhere in this suite, patching
    # config values after the fact wouldn't reach an already-constructed
    # client's own bound `base_url`/`model`. Replacing the module-level
    # `embeddings` NAME itself (which `embed_text` looks up dynamically on
    # every call) with a fresh client pointed at the real Ollama endpoint is
    # the correct patch point here.
    from langchain_openai import OpenAIEmbeddings

    real_embeddings = OpenAIEmbeddings(
        model=ollama_endpoint["embed_model"],
        base_url=ollama_endpoint["openai_api_base"],
        api_key=SecretStr("sk-not-checked-by-ollama"),
        check_embedding_ctx_length=False,
    )
    monkeypatch.setattr(embeddings, "embeddings", real_embeddings)


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
