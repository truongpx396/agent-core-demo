"""Embedding clients: dense (via the LiteLLM proxy) + sparse (local, via
fastembed) + rerank (via a dedicated reranker container) — the three legs
of hybrid retrieval (GRAPH_PATTERNS.md pattern 20).

Dense stays routed through LiteLLM like every other model call (one
gateway, swappable via config, no provider key here). Sparse (BM25) is a
local, deterministic ONNX model — closer in kind to a calculator than a
chat completion — downloading once from Hugging Face and running offline
after that.

Reranking used to be local/fastembed too, but moved to a dedicated
container (`docker-compose.yml`'s `ml-service`; see `ML_SERVICE_URL`'s
config comment for measured numbers) — it was a measured concurrency
bottleneck in-process, worth isolating unlike sparse's cheap lexical scoring.

The fastembed sparse model is lazily constructed (first call pays the
download/load cost, not import time) and cached in a module global — same
reason as `app/agent/runtime.py`'s lazy singleton graph: don't pay startup
cost for a path a process might never exercise.
"""
import httpx
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import (
    EMBED_MODEL,
    ML_SERVICE_URL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    SPARSE_MODEL,
)

embeddings = OpenAIEmbeddings(
    model=EMBED_MODEL,
    base_url=OPENAI_API_BASE,
    api_key=SecretStr(OPENAI_API_KEY),
    check_embedding_ctx_length=False,  # let the proxy/Ollama handle chunking
)


async def embed_text(text: str) -> list[float]:
    return await embeddings.aembed_query(text)


# OpenAIEmbeddings.embed_documents sub-batches at `chunk_size` (1000/request
# default) — tuned for real OpenAI infra, a proven bad fit for the local
# Ollama backend: 1000 real chunks against `ollama_chat/nomic-embed-text`
# broke the endpoint outright (OllamaException: tokenize EOF, litellm
# retries exhausted), and 700 hung past a minute. Swept 100/200/300/500,
# all succeeded (11-29ms/chunk, no clear win past ~200) — 200 is
# comfortably below the last known-good size and clear of the 700+ hang
# zone. Public: `app/ingestion/ingestor.py::ingest_text` reuses this value
# to drive its own batching loop, one `embed_texts` call per batch, for a
# natural per-batch progress checkpoint.
EMBED_BATCH_SIZE = 200


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dense embeddings for MANY texts in one batched round trip — the
    bulk-ingest counterpart to `embed_text`'s single-query call (see
    `EMBED_BATCH_SIZE`'s comment for why the batch size is capped).

    Batching measured ~4x speedup (46ms/chunk one-at-a-time vs ~11ms/chunk
    at 200/request), dominated by per-HTTP-call overhead, not model
    compute — a real 5700-chunk/9.7MB PDF dropped from ~13min to ~75s."""
    return await embeddings.aembed_documents(texts, chunk_size=EMBED_BATCH_SIZE)


_sparse_model = None
_RERANK_TIMEOUT_SECONDS = 10


def _get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding

        _sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)
    return _sparse_model


def embed_sparse(text: str) -> tuple[list[int], list[float]]:
    """BM25 sparse vector as (indices, values) — the shape
    `qdrant_store.py` needs to build a `SparseVector`. Raises on failure
    rather than degrading itself — the caller (`hybrid_search`) owns the
    degrade-to-dense-only policy, so failures must surface here, not be
    swallowed twice.
    """
    vec = next(iter(_get_sparse_model().embed([text])))
    return vec.indices.tolist(), vec.values.tolist()


def embed_sparse_batch(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Batched sparse/BM25 vectors — same local fastembed model as
    `embed_sparse` (fastembed's `.embed()` already batches ONNX inference
    over a list). Raises on failure; degrade-to-dense-only applies to the
    WHOLE batch at once, not per-text — one bad text degrades the entire
    document's sparse leg (see `ingestor.py::_sparse_vectors_or_none`)."""
    return [
        (vec.indices.tolist(), vec.values.tolist())
        for vec in _get_sparse_model().embed(texts)
    ]


async def rerank(query: str, candidates: list[str]) -> list[float]:
    """Cross-encoder relevance scores, one per candidate, same order as
    `candidates` — higher is more relevant. Raises on failure; caller owns
    the degrade policy (see `embed_sparse`'s docstring).

    `async def`, a real HTTP call to the `ml-service` container
    (`ML_SERVICE_URL`) — genuine I/O, belongs on the event loop.

    A fresh `httpx.AsyncClient` per call, not a shared one: `AsyncClient`'s
    pool is bound to whichever loop creates it (same loop-affinity
    constraint as `AsyncPostgresSaver` in `app/agent/runtime.py`), and this
    function gets called from genuinely different loops (the long-running
    loop `retrieve_context` runs on, and the fresh, short-lived loops
    `app/agent/tools.py`'s `_search_docs_impl`/`_skill_search_impl` spin up
    via `_run_with_timeout`'s worker threads) — a shared client would break
    on the second kind.

    Response is `[{"index", "score", ...}]`, placed back into `candidates`'
    order by `index` rather than assumed already sorted (TEI's own
    `/rerank` contract is score-sorted; `ml-service` happens to return
    input order, but reading `index` avoids depending on which).
    `raw_scores` is for wire-compatibility with that TEI contract —
    `ml-service` only ever returns the raw, unbounded logit score
    (`MIN_RERANK_SCORE` in `app/agent/tools.py` is calibrated to that scale).
    """
    async with httpx.AsyncClient(timeout=_RERANK_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{ML_SERVICE_URL}/rerank",
            json={"query": query, "texts": candidates, "raw_scores": True},
        )
    resp.raise_for_status()
    scores = [0.0] * len(candidates)
    for item in resp.json():
        scores[item["index"]] = item["score"]
    return scores
