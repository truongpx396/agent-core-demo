"""Embedding clients: dense (via the LiteLLM proxy) + sparse/rerank (local,
via fastembed) — the two legs of hybrid retrieval (GRAPH_PATTERNS.md
pattern 20).

Dense stays routed through LiteLLM like every other model call in this
app (one gateway, swappable via config, no provider key held here — see
`app/core/config.py`). Sparse (BM25) and reranking are deliberately NOT routed
through LiteLLM: they're local, deterministic, ONNX-based models
(fastembed), not generative calls a gateway's retry/fallback/cost
machinery is built for — closer in kind to `app/agent/tools.py`'s calculator
than to a chat completion. Both download once from Hugging Face and run
fully offline after that, the same "pull once, then local" shape as the
Ollama models this app already depends on.

Both fastembed models are lazily constructed (first call pays the
download/load cost, not import time) and cached in a module global —
mirrors `app/agent/runtime.py`'s lazy singleton graph for the same reason: don't
pay startup cost for a path a given process might never exercise (a test
run that never calls `embed_sparse`/`rerank` never downloads anything).
"""
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import (
    EMBED_MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    RERANK_MODEL,
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
_reranker = None


def _get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding

        _sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)
    return _sparse_model


def _get_reranker():
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _reranker = TextCrossEncoder(model_name=RERANK_MODEL)
    return _reranker


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


def rerank(query: str, candidates: list[str]) -> list[float]:
    """Cross-encoder relevance scores, one per candidate, same order as
    `candidates` — higher is more relevant. Raises on failure; see
    `embed_sparse`'s docstring for why the caller owns the degrade policy."""
    return list(_get_reranker().rerank(query, candidates))
