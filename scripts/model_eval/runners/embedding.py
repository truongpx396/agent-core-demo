"""Evaluating an embedding model for retrieval (specs/014 Story 1, scenario 1).

The question: given this repository's questions and passages, does the model put the passages that answer a question near the top of
what the app would retrieve? Measured at the sizes the app really uses (20 candidates before the reranker, 5 returned) plus the field's
standard NDCG@10 and MRR@10, along with the cost of getting there (query latency, milliseconds per chunk at the app's ingest batch
size, memory, size on disk, vector dimension).

Three backends measure three real paths: the GATEWAY (the app's own client, so what is measured is what the app gets), the SERVICE
(the ML service's /v1/embeddings), and IN-PROCESS (load the ONNX file here, for a model that is not served yet). `evaluate` is a function
of a backend only, so it is tested with a fake one.
"""
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import hardware, public
from scripts.model_eval import metrics as m
from scripts.model_eval.candidates import Candidate
from scripts.model_eval.report import RunOutput
from scripts.model_eval.runners.base import (
    PrerequisiteMissing,
    RunSettings,
    latest_version,
    percentiles,
    public_block,
    public_entry,
    seeded_rng,
    time_ms,
)
from scripts.model_eval.suites import ItemData

DEFAULT_DATASET = "retrieval-domains"
PREFETCH = 20  # the app's HYBRID_PREFETCH_LIMIT: candidates retrieved before the reranker
RETURNED = 5  # the app's RERANK_TOP_K: results finally returned
INGEST_BATCH = 200  # the app's EMBED_BATCH_SIZE, used when the app module cannot be imported (tests)


class EmbeddingBackend(Protocol):
    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray: ...
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...


# ---------------------------------------------------------------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------------------------------------------------------------


class RandomEmbedding:
    """CONTROL: seeded random vectors. Retrieval with these is chance, so any metric that does not say "worse" is broken."""

    identity: str = "random-vectors"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, dim: int = 384, seed: int = 20261017) -> None:
        self.dim, self.seed = dim, seed

    def _vec(self, text: str) -> np.ndarray:
        return seeded_rng(self.seed, text).standard_normal(self.dim).astype(np.float32)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts])

    embed_documents = embed_queries


class GatewayEmbedding:
    """The path the app really uses: its own `OpenAIEmbeddings` client through the LiteLLM `embed` alias, at its ingest batch size.

    The candidate's declared prefixes are applied HERE, to the text sent, so a prefix experiment is two declared candidates over the same
    alias (nomic with and without `search_query:` / `search_document:`). Reading the prefixes from the app's settings instead would make
    a before/after comparison impossible without editing .env between runs."""

    identity: str
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, candidate: Candidate) -> None:
        self._candidate = candidate
        try:
            from app.core.config import EMBED_MODEL
            from app.core.model_resolver import resolve_model
            from app.retrieval import embeddings as app_embeddings
        except Exception as exc:  # noqa: BLE001 - importing the app needs its settings; any failure means the gateway path is unavailable
            raise PrerequisiteMissing(f"cannot import the app's embedding client ({type(exc).__name__}: {exc})") from exc
        self._client = app_embeddings.embeddings
        self._batch = getattr(app_embeddings, "EMBED_BATCH_SIZE", INGEST_BATCH)
        self._loop = asyncio.new_event_loop()  # one loop for the client's life: its connection pool is bound to the loop that made it
        try:
            self._loop.run_until_complete(self._client.aembed_query("probe"))
        except Exception as exc:  # noqa: BLE001 - connection refused, auth, unknown alias ...: all mean the gateway cannot serve embeddings
            raise PrerequisiteMissing(
                f"the gateway did not answer an embedding request ({type(exc).__name__}); is the stack up (`make up`) and OPENAI_API_BASE correct?"
            ) from exc
        resolved = self._loop.run_until_complete(resolve_model(EMBED_MODEL))
        self.identity = resolved or EMBED_MODEL

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        p = self._candidate.prepare.query_prefix
        return np.array([self._loop.run_until_complete(self._client.aembed_query(p + t)) for t in texts], dtype=np.float32)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        p = self._candidate.prepare.document_prefix
        return np.array(self._loop.run_until_complete(self._client.aembed_documents([p + t for t in texts], chunk_size=self._batch)), dtype=np.float32)


class ServiceEmbedding:
    """The ML service's OpenAI-compatible /v1/embeddings (specs/014 contracts/ml-service.md)."""

    identity: str
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, candidate: Candidate, base_url: str | None = None, client=None) -> None:
        import httpx

        from app.core.config import ML_SERVICE_URL

        self._candidate = candidate
        self._model = candidate.source.model or candidate.name
        self._client = client or httpx.Client(base_url=base_url or ML_SERVICE_URL, timeout=60)
        self.identity = f"ml-service:{self._model}"
        try:
            self._post(["probe"])
        except Exception as exc:  # noqa: BLE001 - unreachable, 404 (model not served) or 503 (not ready) all mean this path is unavailable
            raise PrerequisiteMissing(f"the ML service did not serve embeddings for {self._model!r} ({type(exc).__name__}: {exc})") from exc

    def _post(self, texts: Sequence[str]) -> np.ndarray:
        response = self._client.post("/v1/embeddings", json={"model": self._model, "input": list(texts), "encoding_format": "float"})
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        return np.array([d["embedding"] for d in data], dtype=np.float32)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        p = self._candidate.prepare.query_prefix
        return np.concatenate([self._post([p + t for t in texts[i : i + INGEST_BATCH]]) for i in range(0, len(texts), INGEST_BATCH)])

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        p = self._candidate.prepare.document_prefix
        return np.concatenate([self._post([p + t for t in texts[i : i + INGEST_BATCH]]) for i in range(0, len(texts), INGEST_BATCH)])


class InProcessEmbedding:
    """Loads the declared ONNX file here, through the SAME tokenise/pool code the ML service serves with (ml_service.onnx_text), so what
    is measured is what would be served."""

    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def __init__(self, candidate: Candidate, threads: int | None = None) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        from ml_service.onnx_text import OnnxText

        src, prep = candidate.source, candidate.prepare
        assert src.repo and src.file and src.revision  # guaranteed by candidates.parse_candidates for path=inprocess
        before = hardware.rss_mb()
        try:
            model_path = hf_hub_download(src.repo, src.file, revision=src.revision)
            tokenizer_path = hf_hub_download(src.repo, "tokenizer.json", revision=src.revision)
        except Exception as exc:  # noqa: BLE001 - offline and not cached, or a wrong repo/file: the model is not available to load
            raise PrerequisiteMissing(f"cannot fetch {src.repo}@{src.revision[:10]}/{src.file} ({type(exc).__name__}); not cached and no network?") from exc
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        session = ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])
        self._text = OnnxText(session, Tokenizer.from_file(tokenizer_path), pooling=prep.pooling, normalize=prep.normalize,  # type: ignore[arg-type]  # pooling validated against POOLINGS at parse time
                              max_length=prep.max_length, pad_token=prep.pad_token)
        import os

        self.identity, self.revision = f"{src.repo}:{src.file}", src.revision
        self.size_bytes, self.rss_mb = os.path.getsize(model_path), max(hardware.rss_mb() - before, 0.0)
        self._candidate, self.truncated = candidate, 0

    def _embed(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        out = []
        for i in range(0, len(texts), 32):
            encoded = self._text.embed([prefix + t for t in texts[i : i + 32]])
            self.truncated += encoded.truncated
            out.append(encoded.vectors)
        return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed(texts, self._candidate.prepare.query_prefix)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed(texts, self._candidate.prepare.document_prefix)


def build_backend(candidate: Candidate, settings: RunSettings, via: str | None = None) -> EmbeddingBackend:
    path = via or candidate.path
    if candidate.name == "random-vectors":
        return RandomEmbedding(seed=settings.seed)
    if path == "gateway":
        return GatewayEmbedding(candidate)
    if path == "service":
        return ServiceEmbedding(candidate)
    if path == "inprocess":
        return InProcessEmbedding(candidate, settings.threads)
    raise PrerequisiteMissing(f"unknown path {path!r}")


# ---------------------------------------------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    qid: str
    text: str
    relevant: dict[str, int]


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def evaluate(
    backend: EmbeddingBackend,
    corpus_ids: Sequence[str],
    corpus_texts: Sequence[str],
    queries: Sequence[Query],
    *,
    runs: int = 3,
    batch: int = INGEST_BATCH,
) -> tuple[ItemData, dict, list[str], int]:
    """Embed the corpus, then each question on its own (as the app does), rank by cosine similarity, and score every question.
    Returns (per-question data, efficiency, warnings, vector dimension)."""
    t0 = time.perf_counter()
    chunks = []
    for i in range(0, len(corpus_texts), batch):
        chunks.append(backend.embed_documents(corpus_texts[i : i + batch]))
    doc_seconds = time.perf_counter() - t0
    docs = _unit(np.concatenate(chunks))

    latencies: list[float] = []
    query_vecs = np.zeros((len(queries), docs.shape[1]), dtype=np.float32)
    for run in range(max(runs, 1)):
        for qi, q in enumerate(queries):
            vector: list[np.ndarray] = []

            def one(q: Query = q, vector: list[np.ndarray] = vector) -> None:
                vector.append(backend.embed_queries([q.text])[0])

            latencies.append(time_ms(one))
            if run == 0:
                query_vecs[qi] = vector[0]
    sims = _unit(query_vecs) @ docs.T

    ids: list[str] = []
    rows: dict[str, list[float]] = {"recall@5": [], "recall@20": [], "mrr@10": [], "ndcg@10": []}
    dropped = 0
    for qi, q in enumerate(queries):
        order = np.argsort(-sims[qi], kind="stable")[:PREFETCH]
        ranked = [corpus_ids[j] for j in order]
        scores = {
            "recall@5": m.recall_at_k(ranked, q.relevant, RETURNED),
            "recall@20": m.recall_at_k(ranked, q.relevant, PREFETCH),
            "mrr@10": m.mrr_at_k(ranked, q.relevant, 10),
            "ndcg@10": m.ndcg_at_k(ranked, q.relevant, 10),
        }
        if any(v is None for v in scores.values()):
            dropped += 1
            continue
        ids.append(q.qid)
        for name, value in scores.items():
            assert value is not None  # the any(...) check above skipped this query otherwise
            rows[name].append(value)
    warnings = [f"{dropped} question(s) had no relevant passage in the corpus and were left out"] if dropped else []
    data = ItemData(ids, {k: np.array(v, dtype=float) for k, v in rows.items()})
    efficiency = {"query_ms": percentiles(latencies), "ms_per_chunk": doc_seconds * 1000 / max(len(corpus_texts), 1)}
    return data, efficiency, warnings, int(docs.shape[1])


def run(candidate: Candidate, settings: RunSettings, *, backend: EmbeddingBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or DEFAULT_DATASET
    if name.startswith("beir-"):
        entry = public_entry(settings, name)
        beir = public.load_beir(entry, settings.public_root)
        corpus_ids, corpus_texts = list(beir.corpus), list(beir.corpus.values())
        queries = [Query(qid, text, beir.qrels[qid]) for qid, text in beir.queries.items()]
        dataset = {"name": entry.id, "version": entry.sources[0].revision[:12], "split": "test", "items": len(queries), "reviewed_share": 1.0,
                   "public": [public_block(entry)]}
    else:
        manifest = ds.load_verified(latest_version(settings.dataset_root, name))
        corpus = manifest.corpus()
        corpus_ids, corpus_texts = [c["id"] for c in corpus], [c["text"] for c in corpus]
        queries = [Query(r["qid"], r["query"], {k: int(v) for k, v in r["relevant"].items()}) for r in manifest.split_items(settings.split)]
        dataset = ds.dataset_summary(manifest, settings.split)
    backend = backend or build_backend(candidate, settings, via)
    data, efficiency, warnings, dim = evaluate(backend, corpus_ids, corpus_texts, queries, runs=settings.runs)
    truncated = getattr(backend, "truncated", 0)
    if truncated:
        warnings.append(f"{truncated} text(s) exceeded the model's max_length and were truncated")
    efficiency.update({"rss_mb": backend.rss_mb, "run_seconds": time.perf_counter() - started})
    cand = {"name": candidate.name, "identity": backend.identity, "licence": candidate.licence, "path": via or candidate.path,
            "revision": backend.revision, "size_bytes": backend.size_bytes, "dim": dim}
    return RunOutput("embedding", cand, dataset, data, efficiency, warnings)
