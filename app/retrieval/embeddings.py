"""Embedding clients: dense (via the LiteLLM proxy) + sparse (local, via
fastembed) + rerank (via a dedicated reranker container) — the three legs
of hybrid retrieval (GRAPH_PATTERNS.md pattern 20).

Dense stays routed through LiteLLM like every other model call in this
app (one gateway, swappable via config, no provider key held here — see
`app/core/config.py`). Sparse (BM25) is a local, deterministic, ONNX-based
model (fastembed) — not a generative call a gateway's retry/fallback/cost
machinery is built for, closer in kind to `app/agent/tools.py`'s calculator
than to a chat completion — and downloads once from Hugging Face, running
fully offline after that, same "pull once, then local" shape as the Ollama
models this app already depends on.

Reranking used to be local/fastembed too, same shape as sparse, but moved
to a dedicated container (docker-compose.yml's `ml-service`,
`docker/ml-service/main.py`, see `ML_SERVICE_URL`'s own config
comment for the measured numbers) — it was a measured concurrency
bottleneck running in-process, and unlike sparse (cheap, lexical BM25
scoring) it's a real enough per-call cost to be worth genuinely isolating
rather than just better-scheduling.

The fastembed sparse model is lazily constructed (first call pays the
download/load cost, not import time) and cached in a module global —
mirrors `app/agent/runtime.py`'s lazy singleton graph for the same reason:
don't pay startup cost for a path a given process might never exercise (a
test run that never calls `embed_sparse` never downloads anything).
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


def embed_text(text: str) -> list[float]:
    return embeddings.embed_query(text)


# `OpenAIEmbeddings.embed_documents` sub-batches internally at `chunk_size`
# (1000 texts/request, its OWN default) rather than sending everything in
# one request — but that default is tuned for real OpenAI's embedding
# infrastructure, not this app's local Ollama backend, and it's a proven
# bad fit here: a batch of 1000 real chunks against `ollama_chat/
# nomic-embed-text` broke Ollama's embedding endpoint outright
# (`OllamaException - {"error":"Post \"http://127.0.0.1:.../tokenize\":
# EOF"}`, litellm exhausting its own retries before giving up) — and a
# batch of 700 didn't even fail fast, it hung well past a minute (litellm's
# own retry/backoff against an already-broken connection). Empirically
# swept against a real 5700-chunk document: 100/200/300/500 all embedded
# successfully (11-29ms/chunk, no clear win past ~200), so 200 is used
# here — comfortably below the last known-good size (500) and nowhere
# near the 700+ zone that hangs, not a guess at "smaller must be safer."
# Public (not `_`-prefixed): `app/ingestion/ingestor.py::ingest_text` reuses
# this exact value to drive its own outer batching loop directly, one
# `embed_texts` call per batch, so it has a natural per-batch checkpoint to
# report ingest progress from — see that function's `on_progress` param.
EMBED_BATCH_SIZE = 200


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dense embeddings for MANY texts in one batched round trip — the
    bulk-ingest counterpart to `embed_text`'s single-query call
    (`app/ingestion/ingestor.py::ingest_text`, which embeds every chunk of
    a document) — see `EMBED_BATCH_SIZE`'s own comment for why the batch
    size is capped explicitly rather than left at `embed_documents`'s own
    default.

    Live-verified speedup, not a guess: embedding 60 real chunks from this
    app's own test PDF one at a time (`embed_text` in a loop, ingest_text's
    OLD behavior) took 46ms/chunk; the same chunks batched at 200/request
    took ~11ms/chunk — ~4x, dominated by per-HTTP-call overhead (the
    LiteLLM proxy hop + Ollama request handling), not model compute. A
    real ~9.7MB PDF's 5700 chunks were taking ~13 minutes under the old
    per-chunk loop."""
    return embeddings.embed_documents(texts, chunk_size=EMBED_BATCH_SIZE)


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
    `app/retrieval/qdrant_store.py` needs to build a Qdrant `SparseVector`. Raises on
    failure (model load, OOM, ...) rather than degrading itself — the
    caller (`qdrant_store.hybrid_search`) is where the degrade-to-dense-only
    policy actually lives, so failures need to surface here, not be
    swallowed twice.
    """
    vec = next(iter(_get_sparse_model().embed([text])))
    return vec.indices.tolist(), vec.values.tolist()


def embed_sparse_batch(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Batched sparse/BM25 vectors — same local fastembed model as
    `embed_sparse`, batched for the same bulk-ingest reason `embed_texts`
    is (fastembed's own `.embed()` already accepts a list and batches the
    ONNX inference itself). Raises on failure; the caller owns the
    degrade-to-dense-only policy (see `embed_sparse`'s own docstring) —
    here that policy applies to the WHOLE batch at once, not per-text, so
    one bad text degrades the entire document's sparse leg rather than
    just its own point. Acceptable: this local model failing at all
    (versus one text tripping some content-specific edge case) is the
    realistic failure mode — see `app/ingestion/ingestor.py`'s
    `_sparse_vectors_or_none`."""
    return [
        (vec.indices.tolist(), vec.values.tolist())
        for vec in _get_sparse_model().embed(texts)
    ]


async def rerank(query: str, candidates: list[str]) -> list[float]:
    """Cross-encoder relevance scores, one per candidate, same order as
    `candidates` — higher is more relevant. Raises on failure; see
    `embed_sparse`'s docstring for why the caller owns the degrade policy.

    `async def`, a real HTTP call to the `ml-service` container
    (ML_SERVICE_URL) — genuine I/O now, not local ONNX compute, so it
    belongs on the event loop like any other network call (same reasoning
    as app/agent/graph.py's `agent` node calling `llm.ainvoke`).

    A fresh `httpx.AsyncClient` per call, not a shared module-level one:
    `AsyncClient`'s connection pool is bound to whichever event loop
    creates it, same loop-affinity constraint already hit (and documented)
    for `AsyncPostgresSaver` in app/agent/runtime.py — this function gets
    called from genuinely different loops in practice (the long-running
    loop `retrieve_context` runs on, AND the fresh, short-lived loops
    `asyncio.run(...)` creates inside `app/agent/tools.py`'s
    `_search_docs_impl`/`_skill_search_impl`, both dispatched via
    `_run_with_timeout`'s worker threads) — a shared client would break
    the moment it's reused from the second kind.

    The response is `[{"index", "score", ...}]`, placed back into
    `candidates`' own order below by `index` rather than assumed to
    already be in that order — this was TEI's own `/rerank` contract
    (score-sorted, not input-order) and `ml-service` happens to return
    input order already, but reading `index` explicitly means neither
    this function nor its callers depend on which. `raw_scores` is
    accepted for wire-compatibility with that same TEI contract;
    `ml-service` only ever returns the cross-encoder's raw, unbounded
    logit score (MIN_RERANK_SCORE in app/agent/tools.py is calibrated
    against that raw scale, re-verified directly against this model —
    see that constant's own comment).
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
