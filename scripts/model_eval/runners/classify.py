"""Evaluating a router on prompts whose outcomes are known (specs/014 Story 3, T072).

A router answers one question per prompt, "does this prompt need the stronger model?", and what that answer is worth is a fact about two
specific models and a judge, so it can only be measured against LABELLED OUTCOMES (`make router-label`, `routing.py`). The report gives the
yes/no quality (accuracy, macro-F1, calibration) and what the decision is for: how much of the gap between the local and the strong model
comes back as a function of how many prompts are escalated (the cost-quality curve, `suites.ClassifierSuite`), next to a router that picks at
random and one that escalates the longest prompts first. A router that does not beat both is not worth turning on, and the report says so in
numbers.

NO OUTCOMES, NO NUMBER. Without a labelled outcomes file there is nothing to measure routing quality against, and the run ends with the
reason ("unmeasured") and no report at all: a figure computed from nothing would be worse than none (FR-011).

WHAT CAN BE EVALUATED. Anything with a `scores(texts)` method (`ClassifierBackend`): the two built-in controls (`constant`, `random-router`),
the ML service's `/classify` (`service`, which only answers once a head has been accepted there), or a backend handed to `run(backend=...)`.
That last one is the extension point for a new router (for example rules plus topic labels): it is scored, timed and compared exactly like
the others. The trained-head trainer that used to be here was removed on purpose once the first real run showed the labels could not support
any router (ENGINEERING_PATTERNS.md pattern 69); it is in the history of the pull request that removed it.

LATENCY. p50/p95 of one prompt at a time, and a warning when p95 exceeds 50 ms: SC-009's budget for the whole routing call on the reference machine.
"""
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import metrics
from scripts.model_eval.candidates import Candidate
from scripts.model_eval.report import RunOutput
from scripts.model_eval.routing import label, outcomes_dir, read_outcomes
from scripts.model_eval.runners.base import (
    PrerequisiteMissing,
    RunSettings,
    latest_version,
    percentiles,
    seeded_rng,
    time_ms,
)
from scripts.model_eval.suites import ItemData

DEFAULT_DATASET = "route-domains"
LATENCY_BUDGET_MS = 50.0  # SC-009: the whole routing call, p95, on the reference machine
BATCH = 16  # /classify accepts at most sixteen texts per request


class ClassifierBackend(Protocol):
    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def scores(self, texts: Sequence[str]) -> list[float]: ...


class ConstantRouter:
    """CONTROL: a router that never escalates."""

    identity: str = "constant"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def scores(self, texts: Sequence[str]) -> list[float]:
        return [0.0] * len(texts)


class RandomRouter:
    """CONTROL: a router that scores at random."""

    identity: str = "random-router"
    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, seed: int = 20261017) -> None:
        self.seed = seed

    def scores(self, texts: Sequence[str]) -> list[float]:
        return [float(seeded_rng(self.seed, t).random()) for t in texts]


class ServiceClassifier:
    """The ML service's /classify, as app/core/routing_advice.py calls it. It answers only for an ACCEPTED head; otherwise `served_by` is "none"."""

    revision: str | None = None
    size_bytes: int | None = None
    rss_mb: float | None = None

    def __init__(self, candidate: Candidate, client=None) -> None:
        import httpx

        from app.core.config import ML_SERVICE_URL

        self._client = client or httpx.Client(base_url=ML_SERVICE_URL, timeout=60)
        self.identity = f"ml-service:{candidate.source.model or candidate.name}"
        try:
            probe = self._post(["probe"])
        except Exception as exc:  # noqa: BLE001 - unreachable or not ready: this path is unavailable
            raise PrerequisiteMissing(f"the ML service did not answer /classify ({type(exc).__name__}: {exc}); is it up (`make up`)?") from exc
        if probe["served_by"] == "none":
            raise PrerequisiteMissing(
                "the ML service answers /classify with served_by=none: no head is ACCEPTED there, so it has no score to give. There is nothing for "
                "this path to evaluate until a head is accepted; to evaluate another router, pass it to `run(backend=...)`."
            )
        self.revision = probe.get("head_version")

    def _post(self, texts: Sequence[str]) -> dict:
        response = self._client.post("/classify", json={"task": "route", "texts": list(texts)})
        response.raise_for_status()
        return response.json()

    def scores(self, texts: Sequence[str]) -> list[float]:
        out: list[float] = []
        for start in range(0, len(texts), BATCH):
            body = self._post(texts[start : start + BATCH])
            if body["served_by"] == "none" or any(r["score"] is None for r in body["results"]):
                raise PrerequisiteMissing("the ML service stopped giving scores part-way through (served_by=none); the run is not usable")
            out.extend(float(r["score"]) for r in sorted(body["results"], key=lambda r: r["index"]))
        return out


def build_backend(candidate: Candidate, settings: RunSettings, via: str | None = None) -> ClassifierBackend:
    path = via or candidate.path
    if candidate.name == "constant":
        return ConstantRouter()
    if candidate.name == "random-router":
        return RandomRouter(settings.seed)
    if path == "service":
        return ServiceClassifier(candidate)
    raise PrerequisiteMissing(f"a router is evaluated through the controls or path 'service', not {path!r}; to evaluate another router pass it to run(backend=...)")


def find_outcomes(dataset_dir: Path, explicit: Path | None) -> Path | None:
    """The outcomes file to use: the one named, else the only one in the dataset's `outcomes/` directory. Several is ambiguous (they are facts
    about different model pairs), so the operator names one; none means routing quality is unmeasured."""
    if explicit is not None:
        if not explicit.exists():
            raise PrerequisiteMissing(f"--outcomes {explicit} does not exist")
        return explicit
    found = sorted(outcomes_dir(dataset_dir).glob("*.jsonl")) if outcomes_dir(dataset_dir).exists() else []
    if len(found) > 1:
        raise PrerequisiteMissing(f"{len(found)} outcomes files in {outcomes_dir(dataset_dir)} ({', '.join(p.name for p in found)}); pass --outcomes to say which")
    return found[0] if found else None


def evaluate(backend: ClassifierBackend, rows: Sequence[dict], *, runs: int = 3) -> tuple[ItemData, dict]:
    """`rows`: {id, text, label (0/1), q_local, q_strong}. Scores in one batch for the quality figures, then one prompt at a time for latency."""
    texts = [r["text"] for r in rows]
    started = time.perf_counter()
    scores = backend.scores(texts)
    batch_seconds = time.perf_counter() - started
    latencies = []
    for _ in range(max(runs, 1)):
        for text in texts:

            def one(text: str = text) -> None:
                backend.scores([text])

            latencies.append(time_ms(one))
    data = ItemData(
        [r["id"] for r in rows],
        {
            "score": np.array(scores, dtype=float),
            "label": np.array([r["label"] for r in rows], dtype=float),
            "q_local": np.array([r["q_local"] for r in rows], dtype=float),
            "q_strong": np.array([r["q_strong"] for r in rows], dtype=float),
            "words": np.array(metrics.length_router_scores(texts), dtype=float),
        },
    )
    return data, {"query_ms": percentiles(latencies), "ms_per_chunk": batch_seconds * 1000 / max(len(texts), 1)}


def _curve_line(name: str, curve: Sequence[float]) -> str:
    def share(x: float) -> str:
        value = metrics.cpt(curve, x)
        return "never" if value is None else f"{value:.0%}"

    return f"{name}: escalating {share(0.5)} of prompts recovers half the gap and {share(0.9)} recovers 90% (average gap recovered {metrics.apgr(curve):.2f})"


def run(candidate: Candidate, settings: RunSettings, *, backend: ClassifierBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or DEFAULT_DATASET
    version_dir = latest_version(settings.dataset_root, name)
    manifest = ds.load_verified(version_dir)
    rows = manifest.split_items(settings.split)
    outcomes_path = find_outcomes(version_dir, settings.outcomes)
    if outcomes_path is None:
        raise PrerequisiteMissing(
            f"UNMEASURED: there are no routing outcomes for {name} {manifest.version}, so routing quality cannot be stated and no report is written "
            f"(a number computed from nothing would be worse than none). Label the prompts with `make router-label JUDGE=<alias>`; they go to "
            f"{outcomes_dir(version_dir)}."
        )
    outcomes = read_outcomes(outcomes_path)
    usable = [
        {"id": r["id"], "text": r["text"], "label": label(outcomes[r["id"]], settings.margin), "q_local": outcomes[r["id"]].local,
         "q_strong": outcomes[r["id"]].strong, "reviewed": r.get("reviewed")}
        for r in rows
        if r["id"] in outcomes and outcomes[r["id"]].complete
    ]
    if not usable:
        raise PrerequisiteMissing(f"UNMEASURED: none of the {len(rows)} {settings.split} prompts has both scores in {outcomes_path.name}; run `make router-label` for them")
    gap = float(np.mean([u["q_strong"] for u in usable]) - np.mean([u["q_local"] for u in usable]))
    if gap <= 0.0:
        raise PrerequisiteMissing(
            f"UNMEASURED: on these {len(usable)} prompts the strong model did not score above the local one (mean gap {gap:+.3f}), so there is no "
            "quality to recover by routing and a cost-quality curve is undefined. A different model pair or a different judge is needed."
        )
    warnings: list[str] = []
    if len(usable) < len(rows):
        warnings.append(f"{len(rows) - len(usable)} of {len(rows)} {settings.split} prompts have a missing score and were left out, not counted as zero")
    backend = backend or build_backend(candidate, settings, via)
    data, efficiency = evaluate(backend, usable, runs=settings.runs)
    p95 = efficiency["query_ms"]["p95"]
    if p95 > LATENCY_BUDGET_MS:
        warnings.append(f"classifier latency p95 is {p95:.0f} ms, over the {LATENCY_BUDGET_MS:.0f} ms budget (SC-009) on this hardware; the router may not be worth its cost here")
    efficiency.update({"rss_mb": backend.rss_mb, "run_seconds": time.perf_counter() - started})
    q_local, q_strong = data.arrays["q_local"], data.arrays["q_strong"]
    tradeoffs = [
        f"routing outcomes: {outcomes_path.name}, margin {settings.margin}, {int(data.arrays['label'].sum())} of {len(data)} prompts need the stronger model",
        _curve_line("this router", metrics.pgr_curve(data.arrays["score"], q_local, q_strong)),
        _curve_line("random router", metrics.random_router_pgr()),
        _curve_line("longest-first router", metrics.pgr_curve(data.arrays["words"], q_local, q_strong)),
    ]
    dataset = {**ds.dataset_summary(manifest, settings.split), "items": len(usable), "reviewed_share": ds.reviewed_share(usable)}
    cand = {"name": candidate.name, "identity": backend.identity, "licence": candidate.licence, "path": via or candidate.path,
            "revision": backend.revision, "size_bytes": backend.size_bytes, "dim": None}
    return RunOutput("classifier", cand, dataset, data, efficiency, warnings, tradeoffs)
