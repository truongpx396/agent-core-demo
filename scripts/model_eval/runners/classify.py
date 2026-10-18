"""Evaluating a routing classifier on prompts whose outcomes are known (specs/014 Story 3, T072).

A routing head answers one question, "does this prompt need the stronger model?", and what that answer is worth is a fact about two specific
models and a judge, so it can only be measured against LABELLED OUTCOMES (`make router-label`, `routing.py`). The report gives the yes/no
quality (accuracy, macro-F1, calibration) and what the decision is for: how much of the gap between the local and the strong model comes back
as a function of how many prompts are escalated (the cost-quality curve, `suites.ClassifierSuite`), next to a router that picks at random and
one that escalates the longest prompts first. A head that does not beat both is not worth turning on, and the report says so in numbers.

NO OUTCOMES, NO NUMBER. Without a labelled outcomes file there is nothing to measure routing quality against, and the run ends with the
reason ("unmeasured") and no report at all: a figure computed from nothing would be worse than none (FR-011).

TWO PATHS. `inprocess` loads the embedder the ML service loads and a head FILE from `var/model_eval/heads/`: this is how a head is evaluated BEFORE
anyone accepts it, because the service refuses an unaccepted head. `service` asks a running `/classify`, which only answers once a head is
accepted, so it measures what the app would actually get (and the HTTP round trip). The in-process path refuses a head that is not bound to the
exact model revision it loaded, for the same reason the service does.

LATENCY. p50/p95 of one prompt at a time, and a warning when p95 exceeds 50 ms: SC-009's budget for the whole routing call on the reference machine.
"""
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import hardware, metrics, public
from scripts.model_eval.candidates import Candidate
from scripts.model_eval.head import (
    TrainingRefused,
    fit_softmax,
    head_paths,
    l2_normalise,
    service_embedder,
    sigmoid,
    softmax,
    standardise,
)
from scripts.model_eval.report import RunOutput
from scripts.model_eval.routing import label, outcomes_dir, read_outcomes
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
from scripts.model_eval.suites import INTENT_DATASETS, ItemData

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
                "the ML service answers /classify with served_by=none: no head is ACCEPTED there, so it has no score to give. Evaluate a head "
                "file that has not been accepted yet with `--via inprocess`; accept a head only after reading that report."
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


class InProcessClassifier:
    """The embedder the ML service loads plus a head FILE, scored as the service scores: sigmoid(w . x + b) over the L2-normalised embedding."""

    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None

    def __init__(self, candidate: Candidate, head_dir: Path) -> None:
        before = hardware.rss_mb()
        name = candidate.source.model or candidate.name
        try:
            self._embed, info = service_embedder(name)
        except TrainingRefused as exc:
            raise PrerequisiteMissing(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - not cached and no network, or a wrong repo/file
            raise PrerequisiteMissing(f"cannot load {name} the way the ML service loads it ({type(exc).__name__}: {exc})") from exc
        json_path, npz_path = head_paths(head_dir, info.name)
        if not json_path.exists() or not npz_path.exists():
            raise PrerequisiteMissing(f"there is no head for {info.name} in {head_dir}; train one with `make router-head-train MODEL={info.name}`")
        meta = json.loads(json_path.read_text())
        bound = {"model": info.name, "model_repo": info.repo, "model_file": info.file, "model_revision": info.revision, "dim": info.dim}
        wrong = {k: (meta.get(k), v) for k, v in bound.items() if meta.get(k) != v}
        if wrong:  # the service would refuse this head; scoring with it here would report a number the app can never get
            detail = ", ".join(f"{k}: head says {got!r}, loaded model is {want!r}" for k, (got, want) in wrong.items())
            raise PrerequisiteMissing(f"the head in {json_path.name} was not trained for the model just loaded ({detail})")
        with np.load(npz_path, allow_pickle=False) as arrays:
            self._w, self._b = np.asarray(arrays["w"], dtype=np.float64), float(arrays["b"])
        self.accepted = meta.get("accepted") is True
        self.identity, self.revision = f"head:{meta.get('head_version')}", info.revision
        self.size_bytes, self.rss_mb = npz_path.stat().st_size, max(hardware.rss_mb() - before, 0.0)

    def scores(self, texts: Sequence[str]) -> list[float]:
        return [float(s) for s in sigmoid(l2_normalise(np.asarray(self._embed(list(texts)), dtype=float)) @ self._w + self._b)]


def build_backend(candidate: Candidate, settings: RunSettings, via: str | None = None) -> ClassifierBackend:
    path = via or candidate.path
    if candidate.name == "constant":
        return ConstantRouter()
    if candidate.name == "random-router":
        return RandomRouter(settings.seed)
    if path == "service":
        return ServiceClassifier(candidate)
    if path == "inprocess":
        return InProcessClassifier(candidate, settings.head_dir)
    raise PrerequisiteMissing(f"a classifier is evaluated through path 'service' or 'inprocess', not {path!r}")


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


def _pick_oos_threshold(confidence: np.ndarray, oos: np.ndarray) -> float:
    """The confidence below which the head says "none of these", chosen on the VALIDATION split: the value that maximises the average of the
    out-of-scope items caught and the in-scope items kept. The lowest such value wins a tie. Never chosen on the test split."""
    best, best_score = 0.0, -1.0
    for threshold in np.round(np.arange(0.0, 1.0001, 0.01), 2):
        caught = float((confidence[oos] < threshold).mean()) if oos.any() else 0.0
        kept = float((confidence[~oos] >= threshold).mean())
        if (caught + kept) / 2.0 > best_score + 1e-12:
            best, best_score = float(threshold), (caught + kept) / 2.0
    return best


def run_intent_sanity(candidate: Candidate, settings: RunSettings, *, runs: int | None = None) -> RunOutput:
    """The method-sanity check (`suites.IntentSuite`): embed CLINC with the ML service's own pipeline, train a multinomial head on the in-scope
    TRAINING examples, pick the out-of-scope threshold on VALIDATION and report TEST. It needs no stronger model and no labelling."""
    started = time.perf_counter()
    if candidate.name in ("constant", "random-router"):
        raise PrerequisiteMissing(f"{candidate.name!r} is a control for the routing evaluation; it has no embeddings to train an intent head on")
    if (settings.dataset or "") not in INTENT_DATASETS or candidate.path == "service":
        raise PrerequisiteMissing("the intent check trains its own head on an embedding model loaded in this process; use an `inprocess` classifier candidate")
    entry = public_entry(settings, settings.dataset or INTENT_DATASETS[0])
    data = public.load_clinc(entry, settings.public_root)
    try:
        embed, info = service_embedder(candidate.source.model or candidate.name)
    except TrainingRefused as exc:
        raise PrerequisiteMissing(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - not cached and no network, or a wrong repo/file
        raise PrerequisiteMissing(f"cannot load {candidate.name} the way the ML service loads it ({type(exc).__name__}: {exc})") from exc

    def vectors(items: Sequence[public.IntentItem]) -> np.ndarray:
        return l2_normalise(np.asarray(embed([i.text for i in items]), dtype=float))

    in_scope_train = [i for i in data.train if not i.oos]
    x_train, x_val, x_test = standardise(vectors(in_scope_train), vectors(data.validation), vectors(data.test))
    weights, bias = fit_softmax(x_train, np.array([i.intent for i in in_scope_train]), len(data.names))

    def top(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        probs = softmax(x @ weights + bias)
        probs[:, data.oos_index] = 0.0  # the out-of-scope class is never trained on, so it is never a prediction; it is what a low confidence means
        return probs.argmax(axis=1), probs.max(axis=1)

    _, val_confidence = top(x_val)
    threshold = _pick_oos_threshold(val_confidence, np.array([i.oos for i in data.validation]))
    pred, confidence = top(x_test)
    oos = np.array([i.oos for i in data.test])
    item_data = ItemData(
        [i.id for i in data.test],
        {
            "pred": pred.astype(float),
            "label": np.array([-1 if i.oos else i.intent for i in data.test], dtype=float),
            "confidence": confidence,
            "oos": oos.astype(float),
            "oos_threshold": np.full(len(data.test), threshold),
        },
    )
    sample = [i.text for i in data.test[:100]]
    latencies = []
    for _ in range(max(runs if runs is not None else settings.runs, 1)):
        for text in sample:

            def one(text: str = text) -> None:
                embed([text])

            latencies.append(time_ms(one))
    efficiency = {"query_ms": percentiles(latencies), "ms_per_chunk": None, "rss_mb": None, "run_seconds": time.perf_counter() - started}
    in_n = int((~oos).sum())
    tradeoffs = [
        f"method sanity only: a multinomial head trained on {len(in_scope_train)} in-scope CLINC training examples (standardised {info.dim}-d embeddings, "
        f"300 Nesterov steps); it says nothing about routing quality, which needs labelled outcomes",
        f"out-of-scope: {int(oos.sum())} of {len(data.test)} test items are out-of-scope and are scored separately from the {in_n} in-scope ones; "
        f"the rejection threshold {threshold:.2f} was chosen on the validation split",
    ]
    dataset = {"name": entry.id, "version": entry.sources[0].revision[:12], "split": "test", "items": len(data.test), "reviewed_share": 1.0,
               "public": [public_block(entry)]}
    cand = {"name": candidate.name, "identity": f"{info.repo}:{info.file}", "licence": candidate.licence, "path": "inprocess",
            "revision": info.revision, "size_bytes": None, "dim": info.dim}
    return RunOutput("classifier", cand, dataset, item_data, efficiency, [], tradeoffs)


def run(candidate: Candidate, settings: RunSettings, *, backend: ClassifierBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or DEFAULT_DATASET
    if name in INTENT_DATASETS:
        return run_intent_sanity(candidate, settings)
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
    if getattr(backend, "accepted", True) is False:
        warnings.append("this head is NOT accepted: the service refuses it and the app never sees its scores. This report is what a person reads before accepting it")
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
