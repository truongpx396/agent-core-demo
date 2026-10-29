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
reason as `app/agent/runtime/core.py`'s lazy singleton graph: don't pay startup
cost for a path a process might never exercise.
"""
import httpx
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import (
    EMBED_DOCUMENT_PREFIX,
    EMBED_MODEL,
    EMBED_QUERY_PREFIX,
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

# Embedding a QUESTION and embedding a stored PASSAGE are two different operations (specs/014 FR-015). Models trained with task
# prefixes (nomic-embed-text: `search_query:` / `search_document:`) place the two in different regions on purpose, so one function
# serving both roles, which is what this module used to do (`embed_text`), cannot be made right by any prefix. The old names are
# DELETED rather than deprecated: a caller that still uses one is an ImportError, and tests/retrieval/test_embedding_roles.py fails if
# any module under app/ or scripts/ calls the underlying client around these functions.
#
# Both prefixes default to empty (settings: EMBED_QUERY_PREFIX / EMBED_DOCUMENT_PREFIX), so the text sent is byte-identical to before
# until an operator opts in, with a re-embed (app/retrieval/embedding_identity.py).


async def embed_query(text: str) -> list[float]:
    """Embed a search question, or any text compared with other questions (the semantic cache embeds both sides with this)."""
    return await embeddings.aembed_query(EMBED_QUERY_PREFIX + text)


async def embed_document(text: str) -> list[float]:
    """Embed one stored passage, note, memory or skill description."""
    return (await embeddings.aembed_documents([EMBED_DOCUMENT_PREFIX + text], chunk_size=EMBED_BATCH_SIZE))[0]


# OpenAIEmbeddings.embed_documents sub-batches at `chunk_size` (1000/request
# default) — tuned for real OpenAI infra, a proven bad fit for the local
# Ollama backend: 1000 real chunks against `ollama_chat/nomic-embed-text`
# broke the endpoint outright (OllamaException: tokenize EOF, litellm
# retries exhausted), and 700 hung past a minute. Swept 100/200/300/500,
# all succeeded (11-29ms/chunk, no clear win past ~200) — 200 is
# comfortably below the last known-good size and clear of the 700+ hang
# zone. Public: `app/ingestion/ingestor.py::ingest_text` reuses this value
# to drive its own batching loop, one `embed_documents` call per batch, for a
# natural per-batch progress checkpoint.
EMBED_BATCH_SIZE = 200


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Dense embeddings for MANY stored passages in one batched round trip — the bulk-ingest counterpart to `embed_document`'s
    single call (see `EMBED_BATCH_SIZE`'s comment for why the batch size is capped).

    Batching measured ~4x speedup (46ms/chunk one-at-a-time vs ~11ms/chunk
    at 200/request), dominated by per-HTTP-call overhead, not model
    compute — a real 5700-chunk/9.7MB PDF dropped from ~13min to ~75s."""
    return await embeddings.aembed_documents([EMBED_DOCUMENT_PREFIX + t for t in texts], chunk_size=EMBED_BATCH_SIZE)


_embedding_dim: int | None = None


async def embedding_dim() -> int:
    """The dimension of the configured embedding model, read from a real call (never hardcoded, so it cannot drift from whatever the
    alias points at) and cached for the process. Collections, the answer cache and the identity all size themselves from it."""
    global _embedding_dim
    if _embedding_dim is None:
        _embedding_dim = len(await embed_document("dimension probe"))
    return _embedding_dim


def reset_embedding_dim_cache() -> None:
    """For tests and for a process that has just been told the alias moved; production restarts instead."""
    global _embedding_dim
    _embedding_dim = None


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
    """Cross-encoder relevance scores, one per candidate, in `candidates` order (higher is more relevant). Raises
    on failure; the caller owns the degrade policy (see `embed_sparse`). A real HTTP call to the `ml-service`
    container (`ML_SERVICE_URL`), so `async def`.

    A fresh `httpx.AsyncClient` per call, not a shared one: its pool is bound to the loop that creates it (as
    `AsyncPostgresSaver` in `app/agent/runtime/core.py`), and this is called from different loops (the
    long-running one `retrieve_context` uses, and the short-lived ones
    `_search_docs_impl`/`_skill_search_impl` spin up in `run_with_timeout`'s worker threads).

    The response is `[{"index", "score", ...}]`, placed back into `candidates` order by `index` rather than
    assumed sorted (TEI's `/rerank` is score-sorted, `ml-service` returns input order; reading `index` depends
    on neither). `raw_scores` is for wire-compatibility with TEI: `ml-service` returns only the raw logit, the
    scale `MIN_RERANK_SCORE` (app/agent/tools/builtin.py) is calibrated to.
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
