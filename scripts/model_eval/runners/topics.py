"""Scoring a label set on sentences whose topic is known (specs/015 FR-010..012).

The question: given a sentence, does the set's nearest-centroid matcher name the right topic, and does it say "none of these" for a sentence
that belongs to no topic? Scored by `suites.IntentSuite` (in-scope accuracy and macro-F1, and how out-of-scope sentences and in-scope sentences
fare against the cutoff), with the shuffled-predictions control every intent run carries: a matcher whose answers are no better than the same
answers handed to the wrong sentences is reported INVALID.

IT RUNS THE SERVICE'S OWN CODE. The candidate loads its ONNX file here (`InProcessEmbedding`, the same tokenise/pool code the ML service
serves with), embeds the set's examples and the sentences, and calls `ml_service.labelsets.build_index` and `label_results`, the functions
`POST /classify task=label` calls. What is measured is what would be served, without a container and without the HTTP hop. The cutoff is the
one stored in the label set for this candidate's model (`source.model`); a model with no entry cannot be scored, exactly as the service
refuses to serve it.

THE CUTOFF IS NEVER CHOSEN ON THE REPORTED SPLIT. `calibrate` picks it on `dev` (the cutoff that keeps a stated share of in-scope `dev`
sentences) and prints the line to put in the label set; `run` reports on `test` only. Both read the same two functions.

LIMITS, said in every report. The sentences are authored and unreviewed, so the report is PROVISIONAL. `ece_in_scope` treats a cosine similarity
as a probability, which it is not; read accuracy, macro-F1 and the two cutoff rates instead.
"""
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval.candidates import Candidate
from scripts.model_eval.report import RunOutput
from scripts.model_eval.runners.base import (
    PrerequisiteMissing,
    RunSettings,
    latest_version,
    percentiles,
    time_ms,
)
from scripts.model_eval.suites import ItemData

DATASETS = ("route-topics",)  # repository datasets of kind "topics": scored by this module instead of the routing-outcomes runner
LABELSET_DIR = ds.REPO / "docker" / "ml-service" / "labelsets"
DEFAULT_KEEP = 0.95  # the share of in-scope dev sentences the calibrated cutoff must keep


class TopicBackend(Protocol):
    identity: str
    revision: str | None
    size_bytes: int | None
    rss_mb: float | None
    labels: tuple[str, ...]
    labelset_version: str
    min_similarity: float | None

    def nearest(self, texts: Sequence[str]) -> list[tuple[int, float]]:
        """For each text, the index (into `labels`) of the nearest label and its similarity, whether or not it clears the cutoff."""
        ...


class InProcessTopics:
    def __init__(self, candidate: Candidate, labelset, threads: int | None = None) -> None:
        try:
            from ml_service import labelsets as service
        except ImportError as exc:  # the Makefile exports PYTHONPATH with docker/ml-service; a bare `python -m` may not
            raise PrerequisiteMissing(f"cannot import the ML service package ({exc}); run through `make model-eval` or set PYTHONPATH=docker/ml-service") from exc
        from scripts.model_eval.runners.embedding import InProcessEmbedding

        model = candidate.source.model
        if not model:
            raise PrerequisiteMissing(f"candidate {candidate.name!r} declares no source.model; the label set stores its cutoff per model name")
        self._service = service
        self._embedding = InProcessEmbedding(candidate, threads)
        self.identity, self.revision = self._embedding.identity, self._embedding.revision
        self.size_bytes, self.rss_mb = self._embedding.size_bytes, self._embedding.rss_mb
        self.labels = tuple(labelset.labels)
        self.labelset_version = labelset.version
        self.min_similarity = labelset.calibration.get(model)
        examples = [example for ex in labelset.labels.values() for example in ex]
        vectors = self._embedding.embed_documents(examples).astype(np.float64)
        self._index = service.build_index(labelset, vectors, self.revision or "", self.min_similarity if self.min_similarity is not None else 0.5)

    def nearest(self, texts: Sequence[str]) -> list[tuple[int, float]]:
        vectors = self._embedding.embed_queries(texts)
        embedded = [SimpleNamespace(vector=v, truncated=False) for v in vectors]
        results = self._service.label_results(embedded, self._index)
        return [(self.labels.index(r["nearest"]), float(r["similarity"])) for r in results]


def load_labelset(name: str, directory: Path = LABELSET_DIR):
    """The named label set, parsed by the SERVICE's own parser, so a file the service would refuse cannot be scored here."""
    try:
        from ml_service import labelsets as service
    except ImportError as exc:
        raise PrerequisiteMissing(f"cannot import the ML service package ({exc}); run through `make model-eval` or set PYTHONPATH=docker/ml-service") from exc
    path = directory / f"{name}.json"
    if not path.exists():
        raise PrerequisiteMissing(f"no label set named {name!r} at {path}")
    parsed = service.parse_labelset(path.read_text(), name)
    if isinstance(parsed, service.Refused):
        raise PrerequisiteMissing(f"the label set {name!r} is refused by the service's own parser: {parsed.reason}")
    return parsed


def _arrays(backend: TopicBackend, rows: Sequence[dict], none_label: str) -> dict[str, np.ndarray]:
    index = {label: i for i, label in enumerate(backend.labels)}
    matches = backend.nearest([r["text"] for r in rows])
    return {
        "pred": np.array([m[0] for m in matches], dtype=float),
        "label": np.array([-1 if r["label"] == none_label else index[r["label"]] for r in rows], dtype=float),
        "confidence": np.array([m[1] for m in matches], dtype=float),
        "oos": np.array([1.0 if r["label"] == none_label else 0.0 for r in rows]),
    }


def calibrate(backend: TopicBackend, rows: Sequence[dict], none_label: str, keep: float = DEFAULT_KEEP) -> dict:
    """The cutoff that keeps `keep` of the in-scope sentences in `rows` (use the DEV split), rounded DOWN to three decimals so the kept share is
    never below what was asked, and what that cutoff does to the `none` sentences of the same rows."""
    if not 0.0 < keep < 1.0:
        raise ValueError("keep must be strictly between 0 and 1")
    arrays = _arrays(backend, rows, none_label)
    inside = arrays["oos"] == 0
    if not inside.any():
        raise PrerequisiteMissing("the calibration split has no in-scope sentence")
    # The epsilon is float noise: 0.519 can be computed as 0.5189999999999999, and flooring that to three decimals would silently lower the cutoff.
    cutoff = float(np.floor(np.quantile(arrays["confidence"][inside], 1.0 - keep) * 1000.0 + 1e-6) / 1000.0)
    outside = ~inside
    return {
        "min_similarity": cutoff,
        "in_scope_kept": float((arrays["confidence"][inside] >= cutoff).mean()),
        "none_rejected": float((arrays["confidence"][outside] < cutoff).mean()) if outside.any() else None,
        "n_in_scope": int(inside.sum()),
        "n_none": int(outside.sum()),
    }


def _tradeoffs(backend: TopicBackend, arrays: dict[str, np.ndarray], labelset_name: str, n_examples: int) -> list[str]:
    pred, label, oos = arrays["pred"].astype(int), arrays["label"].astype(int), arrays["oos"] == 0
    lines = [f"label set {labelset_name} {backend.labelset_version}: {len(backend.labels)} labels, {n_examples} examples, cutoff {backend.min_similarity} for this model"]
    per_label = []
    for i, name in enumerate(backend.labels):
        mine = oos & (label == i)
        if mine.any():
            per_label.append(f"{name} {int((pred[mine] == i).sum())}/{int(mine.sum())}")
    lines.append("nearest label correct, per topic: " + ", ".join(per_label))
    confusions: dict[tuple[str, str], int] = {}
    for t, p in zip(label[oos], pred[oos], strict=True):
        if t != p:
            key = (backend.labels[t], backend.labels[p])
            confusions[key] = confusions.get(key, 0) + 1
    if confusions:
        top = sorted(confusions.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        lines.append("most common mix-ups (true -> nearest): " + ", ".join(f"{a} -> {b} x{n}" for (a, b), n in top))
    lines.append("ece_in_scope treats a similarity as a probability and is not meaningful here; read accuracy, macro-F1 and the two cutoff rates")
    return lines


def build_backend(candidate: Candidate, labelset, settings: RunSettings, via: str | None = None) -> TopicBackend:
    path = via or candidate.path
    if candidate.name in ("constant", "random-router", "baseline-as-candidate"):
        raise PrerequisiteMissing(f"{candidate.name!r} is a control for routing outcomes; a label set is scored on its own (the shuffled-predictions control is built in)")
    if path != "inprocess":
        raise PrerequisiteMissing(f"a label set is evaluated in-process (path 'inprocess'), not {path!r}: the service answers the same way but does not publish its cutoff")
    return InProcessTopics(candidate, labelset, settings.threads)


def run(candidate: Candidate, settings: RunSettings, *, backend: TopicBackend | None = None, via: str | None = None) -> RunOutput:
    started = time.perf_counter()
    name = settings.dataset or "route-topics"
    version_dir = latest_version(settings.dataset_root, name)
    manifest = ds.load_verified(version_dir)
    if manifest.kind != "topics":
        raise PrerequisiteMissing(f"dataset {name!r} is of kind {manifest.kind!r}, not 'topics'")
    labelset = load_labelset(name)
    if sorted(labelset.labels) != sorted(manifest.raw["labels"]):
        raise PrerequisiteMissing(f"the label set {name!r} and the dataset disagree on the labels: {sorted(labelset.labels)} against {sorted(manifest.raw['labels'])}")
    none_label = manifest.raw["none_label"]
    rows = manifest.split_items(settings.split)
    backend = backend or build_backend(candidate, labelset, settings, via)
    if backend.min_similarity is None:
        raise PrerequisiteMissing(
            f"the label set {name!r} has no calibration for {candidate.source.model!r}, so the service would not serve it with this model either. "
            "Run `python -m scripts.model_eval labelset-calibrate` for it and put the printed line under `calibration`."
        )
    arrays = _arrays(backend, rows, none_label)
    arrays["oos_threshold"] = np.full(len(rows), backend.min_similarity)
    data = ItemData([r["id"] for r in rows], arrays)
    texts = [r["text"] for r in rows]
    batch_started = time.perf_counter()
    backend.nearest(texts)
    batch_seconds = time.perf_counter() - batch_started
    latencies = []
    for _ in range(max(settings.runs, 1)):
        for text in texts:  # one sentence at a time, as the service sees it
            latencies.append(time_ms(partial(backend.nearest, [text])))
    efficiency = {
        "query_ms": percentiles(latencies), "ms_per_chunk": batch_seconds * 1000 / max(len(texts), 1),
        "rss_mb": backend.rss_mb, "run_seconds": time.perf_counter() - started,
    }
    n_examples = sum(len(v) for v in labelset.labels.values())
    dataset = {**ds.dataset_summary(manifest, settings.split), "reviewed_share": ds.reviewed_share(rows)}
    cand = {"name": candidate.name, "identity": backend.identity, "licence": candidate.licence, "path": via or candidate.path,
            "revision": backend.revision, "size_bytes": backend.size_bytes, "dim": None}
    return RunOutput("classifier", cand, dataset, data, efficiency, [], _tradeoffs(backend, arrays, name, n_examples))


def calibration_for(candidate: Candidate, settings: RunSettings, *, name: str = "route-topics", keep: float = DEFAULT_KEEP, backend: TopicBackend | None = None) -> dict:
    """What `labelset-calibrate` prints: the cutoff from the dev split for this candidate's model, ready to paste under `calibration`."""
    version_dir = latest_version(settings.dataset_root, name)
    manifest = ds.load_verified(version_dir)
    labelset = load_labelset(name)
    backend = backend or build_backend(candidate, labelset, settings)
    result = calibrate(backend, manifest.split_items("dev"), manifest.raw["none_label"], keep)
    model = candidate.source.model or candidate.name
    return {"model": model, "labelset": name, "labelset_version": backend.labelset_version, "split": "dev", "keep": keep, **result,
            "calibration_line": {model: {"min_similarity": result["min_similarity"]}}}
