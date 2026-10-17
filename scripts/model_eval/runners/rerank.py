"""Evaluating a cross-encoder reranker (specs/014 Story 1, scenario 4).

A reranker is scored on FIXED first-stage candidate lists of a stated size, never on whatever the current embedding model happens to
retrieve: otherwise a reranker is graded on another model's mistakes, and swapping the embedder would silently move the reranker's
score. Repository-own lists are the gold passages plus the best-ranked wrong ones under a model-independent lexical ranking; public
(BEIR) lists follow the BEIR protocol, the lexical top-K with no gold forced in. Metrics NDCG@10 and MRR@10, with per-list latency.
"""
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import hardware, public
from scripts.model_eval import metrics as m
from scripts.model_eval.candidates import Candidate
from scripts.model_eval.lexical import Bm25
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

DEFAULT_DATASET = "rerank-domains"
PUBLIC_LIST_SIZE = 100  # the BEIR protocol's first-stage depth


class RerankBackend(Protocol):
    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class RandomRerank:
    """CONTROL: random scores. Ranking at random is chance, so a metric that does not say "worse" is broken."""

    identity: str = "random-scores"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, seed: int = 20261017) -> None:
        self.seed = seed

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        return [float(seeded_rng(self.seed, query + "\x00" + t).random()) for t in texts]


class ServiceRerank:
    """The ML service's /rerank, exactly as app/retrieval/embeddings.py::rerank calls it."""

    identity: str
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, candidate: Candidate, client=None) -> None:
        import httpx

        from app.core.config import ML_SERVICE_URL

        self._client = client or httpx.Client(base_url=ML_SERVICE_URL, timeout=60)
        self.identity = f"ml-service:{candidate.source.model or candidate.name}"
        try:
            self.score("probe", ["a", "b"])
        except Exception as exc:  # noqa: BLE001 - unreachable or not ready: this path is unavailable
            raise PrerequisiteMissing(f"the ML service did not answer /rerank ({type(exc).__name__}: {exc}); is it up (`make up`)?") from exc

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        response = self._client.post("/rerank", json={"query": query, "texts": list(texts), "raw_scores": True})
        response.raise_for_status()
        scores = [0.0] * len(texts)
        for item in response.json():
            scores[item["index"]] = float(item["score"])
        return scores


class InProcessRerank:
    """A cross-encoder ONNX file loaded here: (query, passage) pairs through the model, one logit each."""

    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def __init__(self, candidate: Candidate, threads: int | None = None) -> None:
        import os

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        from ml_service.onnx_text import prepare_tokenizer

        src, prep = candidate.source, candidate.prepare
        assert src.repo and src.file and src.revision
        before = hardware.rss_mb()
        try:
            model_path = hf_hub_download(src.repo, src.file, revision=src.revision)
            tokenizer_path = hf_hub_download(src.repo, "tokenizer.json", revision=src.revision)
        except Exception as exc:  # noqa: BLE001 - not cached and no network, or a wrong repo/file
            raise PrerequisiteMissing(f"cannot fetch {src.repo}@{src.revision[:10]}/{src.file} ({type(exc).__name__})") from exc
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])
        self._tokenizer = prepare_tokenizer(Tokenizer.from_file(tokenizer_path), max_length=prep.max_length, pad_token=prep.pad_token)
        self._inputs = [i.name for i in self._session.get_inputs()]
        self.identity, self.revision = f"{src.repo}:{src.file}", src.revision
        self.size_bytes, self.rss_mb = os.path.getsize(model_path), max(hardware.rss_mb() - before, 0.0)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(texts), 32):
            enc = self._tokenizer.encode_batch([(query, t) for t in texts[i : i + 32]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            logits = np.asarray(self._session.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0])
            out.extend(float(x) for x in logits.reshape(len(enc), -1)[:, 0])
        return out


def build_backend(candidate: Candidate, settings: RunSettings, via: str | None = None) -> RerankBackend:
    path = via or candidate.path
    if candidate.name == "random-scores":
        return RandomRerank(settings.seed)
    if path == "service":
        return ServiceRerank(candidate)
    if path == "inprocess":
        return InProcessRerank(candidate, settings.threads)
    raise PrerequisiteMissing(f"a reranker is evaluated through path 'service' or 'inprocess', not {path!r}")


@dataclass(frozen=True)
class CandidateList:
    qid: str
    query: str
    ids: list[str]
    texts: list[str]
    gains: dict[str, int]  # relevant id -> gain


def evaluate(backend: RerankBackend, lists: Sequence[CandidateList], *, runs: int = 3) -> tuple[ItemData, dict, list[str]]:
    ids, ndcg, mrr, latencies, scored, dropped = [], [], [], [], 0, 0
    wall = time.perf_counter()
    for run in range(max(runs, 1)):
        for item in lists:
            holder: list[list[float]] = []

            def one(item: CandidateList = item, holder: list[list[float]] = holder) -> None:
                holder.append(backend.score(item.query, item.texts))

            latencies.append(time_ms(one))
            scored += len(item.texts)
            if run == 0:
                order = np.argsort(-np.asarray(holder[0]), kind="stable")
                ranked = [item.ids[j] for j in order]
                n, r = m.ndcg_at_k(ranked, item.gains, 10), m.mrr_at_k(ranked, item.gains, 10)
                if n is None or r is None:
                    dropped += 1
                    continue
                ids.append(item.qid)
                ndcg.append(n)
                mrr.append(r)
    elapsed = time.perf_counter() - wall
    warnings = [f"{dropped} list(s) contained no relevant passage and were left out"] if dropped else []
    data = ItemData(ids, {"ndcg@10": np.array(ndcg), "mrr@10": np.array(mrr)})
    return data, {"query_ms": percentiles(latencies), "ms_per_chunk": elapsed * 1000 / max(scored, 1)}, warnings


def _repo_lists(manifest: ds.Manifest, split: str) -> list[CandidateList]:
    return [
        CandidateList(r["qid"], r["query"], [c["id"] for c in r["candidates"]], [c["text"] for c in r["candidates"]],
                      {c["id"]: int(c["gain"]) for c in r["candidates"] if int(c["gain"]) > 0})
        for r in manifest.split_items(split)
    ]


def _public_lists(beir: public.BeirData, k: int) -> list[CandidateList]:
    doc_ids, doc_texts = list(beir.corpus), list(beir.corpus.values())
    index = Bm25(doc_texts)
    out = []
    for qid, text in beir.queries.items():
        top = index.top(text, k)
        out.append(CandidateList(qid, text, [doc_ids[j] for j in top], [doc_texts[j] for j in top], beir.qrels[qid]))
    return out


def run(candidate: Candidate, settings: RunSettings, *, backend: RerankBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or DEFAULT_DATASET
    if name.startswith("beir-"):
        entry = public_entry(settings, name)
        k = settings.list_size or PUBLIC_LIST_SIZE
        lists = _public_lists(public.load_beir(entry, settings.public_root), k)
        dataset = {"name": entry.id, "version": f"{entry.sources[0].revision[:12]}-k{k}", "split": "test", "items": len(lists),
                   "reviewed_share": 1.0, "public": [public_block(entry)]}
    else:
        manifest = ds.load_verified(latest_version(settings.dataset_root, name))
        lists = _repo_lists(manifest, settings.split)
        dataset = ds.dataset_summary(manifest, settings.split)
    backend = backend or build_backend(candidate, settings, via)
    data, efficiency, warnings = evaluate(backend, lists, runs=settings.runs)
    efficiency.update({"rss_mb": backend.rss_mb, "run_seconds": time.perf_counter() - started})
    cand = {"name": candidate.name, "identity": backend.identity, "licence": candidate.licence, "path": via or candidate.path,
            "revision": backend.revision, "size_bytes": backend.size_bytes, "dim": None}
    return RunOutput("reranker", cand, dataset, data, efficiency, warnings)
