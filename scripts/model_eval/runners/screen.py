"""Evaluating the prompt-injection / jailbreak screen (specs/014 Story 1, scenario 5).

Vendor cards report detection rate at a fixed false-positive rate on their own private data. That is necessary and not sufficient for
this app: what hurts here is a screen that blocks an ordinary support or ops question, especially one that merely TALKS about
instructions and policies. So alongside ROC-AUC and detection at 1% FPR the report gives the false-positive rate on benign prompts in
this repository's own domains at the threshold the app actually uses, and the same figures for the app's real decision (the regex layer
OR the model), which is what a person using the product experiences.
"""
import time
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import hardware, public
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

DEFAULT_DATASET = "screen-domains"


class ScreenBackend(Protocol):
    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def malicious_scores(self, texts: Sequence[str]) -> list[float]: ...


class AlwaysBenign:
    """CONTROL: a screen that never flags anything."""

    identity: str = "always-benign"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def malicious_scores(self, texts: Sequence[str]) -> list[float]:
        return [0.0] * len(texts)


class RandomScreen:
    """CONTROL: a screen that scores at random."""

    identity: str = "random-scores"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, seed: int = 20261017) -> None:
        self.seed = seed

    def malicious_scores(self, texts: Sequence[str]) -> list[float]:
        return [float(seeded_rng(self.seed, t).random()) for t in texts]


class ServiceScreen:
    """The ML service's /prompt-guard, as app/core/moderation.py calls it."""

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
            self.malicious_scores(["probe"])
        except Exception as exc:  # noqa: BLE001 - unreachable or not ready: this path is unavailable
            raise PrerequisiteMissing(f"the ML service did not answer /prompt-guard ({type(exc).__name__}: {exc}); is it up (`make up`)?") from exc

    def malicious_scores(self, texts: Sequence[str]) -> list[float]:
        response = self._client.post("/prompt-guard", json={"texts": list(texts)})
        response.raise_for_status()
        return [float(r["malicious_score"]) for r in sorted(response.json(), key=lambda r: r["index"])]


class InProcessScreen:
    """A sequence-classification ONNX file loaded here through the ML service's own text path; malicious_score = softmax(logits)[1]."""

    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def __init__(self, candidate: Candidate, threads: int | None = None) -> None:
        import os

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        from ml_service.guard import softmax
        from ml_service.onnx_text import OnnxText

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
        session = ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])
        self._text = OnnxText(session, Tokenizer.from_file(tokenizer_path), pooling="none", normalize=False, max_length=prep.max_length, pad_token=prep.pad_token)
        self._softmax = softmax
        self.identity, self.revision = f"{src.repo}:{src.file}", src.revision
        self.size_bytes, self.rss_mb = os.path.getsize(model_path), max(hardware.rss_mb() - before, 0.0)

    def malicious_scores(self, texts: Sequence[str]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(texts), 32):
            out.extend(float(p[1]) for p in self._softmax(self._text.logits(list(texts[i : i + 32]))))
        return out


def build_backend(candidate: Candidate, settings: RunSettings, via: str | None = None) -> ScreenBackend:
    path = via or candidate.path
    if candidate.name == "always-benign":
        return AlwaysBenign()
    if candidate.name == "random-scores":
        return RandomScreen(settings.seed)
    if path == "service":
        return ServiceScreen(candidate)
    if path == "inprocess":
        return InProcessScreen(candidate, settings.threads)
    raise PrerequisiteMissing(f"a screen is evaluated through path 'service' or 'inprocess', not {path!r}")


def pattern_layer_blocks(text: str) -> bool:
    """Would the app's regex / denylist layer alone refuse this text? The same patterns app/core/moderation.py::screen runs first.
    (They are module-private there; tests/scripts/model_eval/test_runners_screen.py pins that this agrees with `screen` itself.)"""
    from app.core import moderation

    return any(p.search(text) for p in (*moderation._INJECTION_PATTERNS, *moderation._DENYLIST_PATTERNS))  # noqa: SLF001


def app_threshold() -> float:
    from app.core.moderation import ML_INJECTION_THRESHOLD

    return ML_INJECTION_THRESHOLD


def evaluate(backend: ScreenBackend, items: Sequence[dict], *, runs: int = 3) -> tuple[ItemData, dict, list[str]]:
    """`items`: {id, text, label (1 attack / 0 benign), domain (1 = counted in the in-domain false-positive rate)}."""
    texts = [i["text"] for i in items]
    t0 = time.perf_counter()
    scores = backend.malicious_scores(texts)
    batch_seconds = time.perf_counter() - t0
    latencies = []
    for _ in range(max(runs, 1)):
        for text in texts:

            def one(text: str = text) -> None:
                backend.malicious_scores([text])

            latencies.append(time_ms(one))
    data = ItemData(
        [i["id"] for i in items],
        {
            "score": np.array(scores, dtype=float),
            "label": np.array([i["label"] for i in items], dtype=float),
            "domain": np.array([i["domain"] for i in items], dtype=float),
            "pattern_hit": np.array([1.0 if pattern_layer_blocks(t) else 0.0 for t in texts]),
        },
    )
    return data, {"query_ms": percentiles(latencies), "ms_per_chunk": batch_seconds * 1000 / max(len(texts), 1)}, []


def run(candidate: Candidate, settings: RunSettings, *, backend: ScreenBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or DEFAULT_DATASET
    if name in ("deepset-prompt-injections", "jackhhao-jailbreak"):
        entry = public_entry(settings, name)
        # Public sets have no "domain" split, so every item counts toward the false-positive rate for this run.
        items = [{"id": p.id, "text": p.text, "label": p.label, "domain": 1} for p in public.load_prompt_injection(entry, settings.public_root)]
        dataset = {"name": entry.id, "version": entry.sources[0].revision[:12], "split": "test", "items": len(items), "reviewed_share": 1.0,
                   "public": [public_block(entry)]}
    else:
        manifest = ds.load_verified(latest_version(settings.dataset_root, name))
        rows = manifest.split_items(settings.split)
        items = [{"id": r["id"], "text": r["text"], "label": 1 if r["label"] == "attack" else 0, "domain": 1} for r in rows]
        dataset = ds.dataset_summary(manifest, settings.split)
    backend = backend or build_backend(candidate, settings, via)
    data, efficiency, warnings = evaluate(backend, items, runs=settings.runs)
    efficiency.update({"rss_mb": backend.rss_mb, "run_seconds": time.perf_counter() - started})
    cand = {"name": candidate.name, "identity": backend.identity, "licence": candidate.licence, "path": via or candidate.path,
            "revision": backend.revision, "size_bytes": backend.size_bytes, "dim": None}
    return RunOutput("screen", cand, dataset, data, efficiency, warnings)
