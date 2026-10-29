"""A real Qdrant + real embedding-model round trip for app/retrieval/qdrant_store.py: the fake-free counterpart
to tests/retrieval/test_qdrant_store.py, which (correctly, for a fast hermetic suite) mocks `get_client()` and
proves only that the right `collection_name`/args reach a fake client. What that can't catch is a
qdrant-client/qdrant-server wire-protocol mismatch (named dense+sparse vectors,
`FusionQuery(fusion=Fusion.RRF)`, `Prefetch`) breaking silently across a dependency bump. That needs a real
server, hence `@pytest.mark.llm` (tests/containers.py's docstring explains why it self-skips without Docker
instead of needing `make up`).

`@pytest.mark.llm`, not `integration`: it moved here from tests/integration/ after a CI run caught
`openai.APIConnectionError`. `app.retrieval.embeddings.embed_text` (the dense leg) is a REAL network call to
an embedding API ("Dense stays routed through LiteLLM like every other model call in this app", per
`embeddings.py`), NOT the local fastembed model this file originally and wrongly assumed. Only `embed_sparse`
is local; `rerank` moved to a dedicated `ml-service` container (see its docstring), which this test's
`hybrid_search` calls need reachable at `ML_SERVICE_URL`, as in the real app. The `test-integration` job
provisions no LLM/embedding backend (that is why it's cheap), so this needs `tests/live/`'s `ollama_endpoint`
fixture (a real `nomic-embed-text`, in the container `test_agent_tool_calling.py`'s chat model shares; see
tests/containers.py::ensure_ollama) alongside `ensure_qdrant()`.

It uses the REAL `hybrid_search`/`ensure_collection`/`build_point`/`upsert`, not hand-rolled vectors that
bypass them: it is the one test exercising the exact code path `search_docs` runs in production (dense+sparse
fusion and reranking included) instead of a mock proving the right arguments were passed.
`embed_text`/`ensure_collection`/`upsert`/`hybrid_search` are `async def`, so both tests are too and await
each real I/O call directly (`embed_sparse` stays sync: a local CPU-bound fastembed model).
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


async def test_hybrid_search_round_trips_through_a_real_server():
    collection = "integration-test-docs"
    dense_vector = await embeddings.embed_document("checkpointers persist LangGraph state")
    await qdrant_store.ensure_collection(dim=len(dense_vector), collection=collection)

    sparse_indices, sparse_values = embeddings.embed_sparse(
        "checkpointers persist LangGraph state"
    )
    point = qdrant_store.build_point(
        point_id=1,
        dense_vector=dense_vector,
        payload={"text": "A checkpointer persists LangGraph state across turns.", "tenant": "ecorp"},
        sparse_vector=(sparse_indices, sparse_values),
    )
    await qdrant_store.upsert([point], collection=collection)

    results = await qdrant_store.hybrid_search("what persists state in LangGraph?", collection=collection)

    assert len(results) >= 1
    assert "checkpointer" in results[0].payload["text"].lower()


async def test_a_point_with_no_sparse_vector_is_still_found_via_the_dense_leg():
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
    dense_vector = await embeddings.embed_document("Ecorp support hours are 9am to 5pm weekdays")
    await qdrant_store.ensure_collection(dim=len(dense_vector), collection=collection)

    point = qdrant_store.build_point(
        point_id=1,
        dense_vector=dense_vector,
        payload={"text": "Ecorp support hours are 9am to 5pm on weekdays.", "tenant": "ecorp"},
        sparse_vector=None,
    )
    await qdrant_store.upsert([point], collection=collection)

    results = await qdrant_store.hybrid_search("when is Ecorp support available?", collection=collection)

    assert len(results) >= 1
    assert "9am" in results[0].payload["text"]
