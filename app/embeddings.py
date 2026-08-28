"""Embedding clients: dense (via the LiteLLM proxy) + sparse/rerank (local,
via fastembed) — the two legs of hybrid retrieval (GRAPH_PATTERNS.md
pattern 20).

Dense stays routed through LiteLLM like every other model call in this
app (one gateway, swappable via config, no provider key held here — see
`app/config.py`). Sparse (BM25) and reranking are deliberately NOT routed
through LiteLLM: they're local, deterministic, ONNX-based models
(fastembed), not generative calls a gateway's retry/fallback/cost
machinery is built for — closer in kind to `app/tools.py`'s calculator
than to a chat completion. Both download once from Hugging Face and run
fully offline after that, the same "pull once, then local" shape as the
Ollama models this app already depends on.

Both fastembed models are lazily constructed (first call pays the
download/load cost, not import time) and cached in a module global —
mirrors `app/agent.py`'s lazy singleton graph for the same reason: don't
pay startup cost for a path a given process might never exercise (a test
run that never calls `embed_sparse`/`rerank` never downloads anything).
"""
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.config import (
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
    `app/qdrant_store.py` needs to build a Qdrant `SparseVector`. Raises on
    failure (model load, OOM, ...) rather than degrading itself — the
    caller (`qdrant_store.hybrid_search`) is where the degrade-to-dense-only
    policy actually lives, so failures need to surface here, not be
    swallowed twice.
    """
    vec = next(iter(_get_sparse_model().embed([text])))
    return vec.indices.tolist(), vec.values.tolist()


def rerank(query: str, candidates: list[str]) -> list[float]:
    """Cross-encoder relevance scores, one per candidate, same order as
    `candidates` — higher is more relevant. Raises on failure; see
    `embed_sparse`'s docstring for why the caller owns the degrade policy."""
    return list(_get_reranker().rerank(query, candidates))
