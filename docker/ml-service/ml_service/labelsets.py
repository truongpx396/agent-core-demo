"""Which of a few predefined choices is this text closest to? The matcher behind `POST /v1/decide` (decide.py;
specs/015 contracts/label-sets.md, specs/016 contracts/decide.md). It knows nothing of HTTP or the wire shape:
sets, indexes, cutoffs, failover.

A *label set* is a reviewed JSON file: a name, a few labels with example sentences, and for each serving model
the similarity below which a text is "none of these". Nothing is trained. Per model, the service embeds the
examples, averages each label's unit vectors into a *centroid*, and answers with the label whose centroid is
nearest by cosine, or `null` when even the nearest is below the model's cutoff. The embedder is the
`EmbedService` behind `/v1/embeddings`, so one model loaded for two roles is loaded once.

Why a centroid: measured on CLINC150 with bge-small, it beat "nearest example" and "mean of the two nearest"
at every k (accuracy 0.863 vs 0.806 and 0.834 at k=10) and rejected more out-of-scope text at the same keep
rate (specs/015 D1). It assumes a label is ONE cluster of meaning; a label that is really two topics should be
two labels.

Four rules keep a set from doing harm, each a refusal at load or build time rather than a hope at request
time:
1. The examples are TEXT, embedded by the serving model itself. A stored vector is valid for one model
   revision and a wrong one gives confident nonsense with no error; text cannot go stale (D3). The index is
   rebuilt when a model reloads under a new revision.
2. The cutoff lives in the set, PER MODEL: similarity scales differ and the fallback is a different model. A
   model with no `calibration` entry does not serve that set (`refused: no calibration for <model>`); it never
   gets a guessed default (D2).
3. A set file is data, never code: strict JSON, every field checked. A bad file is `refused: <reason>` for
   that set in `/health/models` and nothing else; the other sets, `/v1/embeddings`, the reranker and the guard
   serve as usual.
4. The service returns FACTS (`label`, `similarity`, `runner_up`, `margin`), never a decision about them (D4).

Failover is the classifier's: the `classify`-role models in `models.toml` order are the chain. A model that
raises goes through the registry's `failed` state; one that only overruns `ML_CLASSIFY_DEADLINE_MS` is skipped
for that request and stays `ready`. When nothing can answer, `decide()` is None and decide.py returns 503
`model_unavailable`. A request naming several sets is answered by ONE model holding an index for all of them;
the deadline is per model, so a primary and a fallback that both overrun can spend twice it.

No request text is logged: logs carry set, model and exception-class names; metrics carry the set and
`served_by`.
"""
import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ml_service.metrics import Metrics
from ml_service.registry import ModelSpec, Registry
from ml_service.settings import Settings

logger = logging.getLogger("ml_service.labelsets")

SCHEMA_VERSION = 1
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LABEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_LABELS, MAX_LABELS = 2, 64
MIN_EXAMPLES, MAX_EXAMPLES = 3, 64
MAX_EXAMPLE_CHARS = 300
MAX_TOTAL_EXAMPLES = 512
BUILD_TIMEOUT_S = 30.0  # embedding every example of one set with one model; a wait that can end only when a counterparty acts has a ceiling
ROLES = ("primary", "fallback")


@dataclass(frozen=True)
class LabelSet:
    name: str
    description: str
    labels: dict[str, tuple[str, ...]]
    calibration: dict[str, float]  # model name -> min_similarity
    version: str


@dataclass(frozen=True)
class Refused:
    reason: str  # the text after "refused: " in /health/models


def _normal(example: str) -> str:
    return " ".join(example.casefold().split())


def _version(name: str, labels: dict[str, tuple[str, ...]], calibration: dict[str, float]) -> str:
    """12 hex characters of the sha256 of the canonical content, EXCLUDING the description: everything that can change an answer changes the
    version, and a reworded description does not."""
    canonical = json.dumps(
        {"schema_version": SCHEMA_VERSION, "name": name, "labels": {k: list(v) for k, v in labels.items()}, "calibration": calibration},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def parse_labelset(text: str, expected_name: str) -> LabelSet | Refused:
    """Parse and validate one set file's text. Every rule here is a refusal at load time, not a surprise at request time. Never raises."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return Refused(f"malformed ({type(exc).__name__} reading {expected_name}.json)")
    if not isinstance(raw, dict):
        return Refused("malformed (not a JSON object)")
    if raw.get("schema_version") != SCHEMA_VERSION:
        return Refused(f"malformed (schema_version {raw.get('schema_version')!r}, this service reads {SCHEMA_VERSION})")
    name = raw.get("name")
    if name != expected_name or not isinstance(name, str) or not NAME.match(name):
        return Refused(f"malformed (name must equal the file name {expected_name!r} and match {NAME.pattern})")
    description = raw.get("description", "")
    if not isinstance(description, str):
        return Refused("malformed (description must be a string)")
    labels_raw = raw.get("labels")
    if not isinstance(labels_raw, dict) or not MIN_LABELS <= len(labels_raw) <= MAX_LABELS:
        return Refused(f"malformed (labels must be an object of {MIN_LABELS}..{MAX_LABELS} labels)")
    labels: dict[str, tuple[str, ...]] = {}
    seen: dict[str, str] = {}
    total = 0
    for label, examples in labels_raw.items():
        if not isinstance(label, str) or not LABEL_NAME.match(label):
            return Refused(f"malformed (label name must match {LABEL_NAME.pattern})")
        if not isinstance(examples, list) or not MIN_EXAMPLES <= len(examples) <= MAX_EXAMPLES:
            return Refused(f"malformed (label {label!r} needs {MIN_EXAMPLES}..{MAX_EXAMPLES} examples)")
        for example in examples:
            if not isinstance(example, str) or not example.strip() or len(example) > MAX_EXAMPLE_CHARS:
                return Refused(f"malformed (label {label!r} has an example that is not a string of 1..{MAX_EXAMPLE_CHARS} characters)")
            key = _normal(example)
            if key in seen:
                where = f"twice under {label!r}" if seen[key] == label else f"under both {seen[key]!r} and {label!r}"
                return Refused(f"malformed (an example appears {where}; two labels sharing a sentence can never be told apart)")
            seen[key] = label
        total += len(examples)
        labels[label] = tuple(examples)
    if total > MAX_TOTAL_EXAMPLES:
        return Refused(f"malformed ({total} examples; the limit is {MAX_TOTAL_EXAMPLES})")
    calibration_raw = raw.get("calibration")
    if not isinstance(calibration_raw, dict) or not calibration_raw:
        return Refused("malformed (calibration must be an object with one entry per serving model)")
    calibration: dict[str, float] = {}
    for model, entry in calibration_raw.items():
        value = entry.get("min_similarity") if isinstance(entry, dict) else None
        if not isinstance(model, str) or not isinstance(value, int | float) or not 0.0 < value < 1.0:  # True and False are 1 and 0, which the range excludes
            return Refused(f"malformed (calibration for {model!r} needs min_similarity as a number strictly between 0 and 1)")
        calibration[model] = float(value)
    return LabelSet(name, description, labels, calibration, _version(name, labels, calibration))


def read_labelsets(directory: Path) -> dict[str, LabelSet | Refused]:
    """Every `*.json` in `directory`, keyed by file stem. Never raises: an unreadable directory or file is a `Refused`, because a missing
    routing file must not stop the embeddings, the reranker or the guard from starting (the same CI-found failure the head reader guards)."""
    try:
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    except OSError as exc:
        return {"(directory)": Refused(f"unreadable ({type(exc).__name__} listing the label-set directory; it must be readable by the service's user)")}
    out: dict[str, LabelSet | Refused] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            out[path.stem] = Refused(f"unreadable ({type(exc).__name__} reading {path.name})")
            continue
        out[path.stem] = parse_labelset(text, path.stem)
    return out


@dataclass(frozen=True)
class Index:
    """One set as one model sees it: a unit centroid per label."""

    revision: str  # the model revision the weights were really read from
    labels: tuple[str, ...]
    centroids: np.ndarray  # float64 (n_labels, dim), rows L2-normalised
    min_similarity: float


@dataclass(frozen=True)
class Answer:
    served_by: str  # "primary" | "fallback" | "none"
    model: str | None
    labelset_version: str | None
    results: list[dict[str, Any]]


@dataclass(frozen=True)
class Decided:
    """What `POST /v1/decide` is built from: one text, answered against one or more sets by one model."""

    served_by: str  # "primary" | "fallback"
    model: str
    versions: dict[str, str]  # set name -> the set's version
    results: dict[str, dict[str, Any]]  # set name -> the result for the one text
    tokens: int  # tokens the serving model actually embedded (after truncation)


@dataclass(frozen=True)
class _Hit:
    served_by: str
    model: str
    embedded: list[Any]
    results: dict[str, list[dict[str, Any]]]  # set name -> one result per embedded text


def _unit(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0.0, 1.0, norms)


def build_index(labelset: LabelSet, vectors: np.ndarray, revision: str, min_similarity: float) -> Index:
    """The centroids, from the embedded examples in the order `labelset.labels` lists them. Raises ValueError for anything that would make an
    answer meaningless (a non-finite vector, a label whose examples cancel out), so the caller can fail the build instead of serving it."""
    if not np.isfinite(vectors).all():
        raise ValueError("non-finite example vector")
    unit = _unit(vectors)
    rows: list[np.ndarray] = []
    start = 0
    for label, examples in labelset.labels.items():
        mean = unit[start : start + len(examples)].mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm < 1e-6:
            raise ValueError(f"the examples of label {label!r} cancel out")
        rows.append(mean / norm)
        start += len(examples)
    return Index(revision, tuple(labelset.labels), np.stack(rows), min_similarity)


def label_results(embedded: list[Any], index: Index) -> list[dict[str, Any]]:
    """One result per text. Raises ValueError on a non-finite similarity so a glitching model reaches the fallback (JSON cannot carry NaN)."""
    vectors = _unit(np.stack([np.asarray(e.vector, dtype=np.float64) for e in embedded]))
    similarities = vectors @ index.centroids.T
    if not np.isfinite(similarities).all():
        raise ValueError("non-finite similarity")
    results = []
    for i, (row, e) in enumerate(zip(similarities, embedded, strict=True)):
        order = np.argsort(-row, kind="stable")
        best, second = int(order[0]), int(order[1])
        similarity = float(row[best])
        results.append(
            {
                "index": i,
                "label": index.labels[best] if similarity >= index.min_similarity else None,
                "nearest": index.labels[best],  # the label the cutoff abstained from, for a caller that wants to log or inspect the near miss
                "similarity": similarity,
                "runner_up": index.labels[second],
                "margin": similarity - float(row[second]),
                "truncated": bool(e.truncated),
            }
        )
    return results


class Labeller:
    def __init__(
        self, registry: Registry, specs: list[ModelSpec], sets: dict[str, LabelSet | Refused], settings: Settings, metrics: Metrics
    ) -> None:
        self._registry = registry
        self._order = [s.name for s in specs if "classify" in s.roles]
        self._sets = sets
        self._deadline_s = settings.classify_deadline_ms / 1000
        self._metrics = metrics
        self._indexes: dict[tuple[str, str], Index] = {}
        self._why: dict[tuple[str, str], str] = {}  # the status line of a (set, model) that has no index: pending or refused
        self._lock = asyncio.Lock()

    def knows(self, name: str) -> bool:
        return name in self._sets

    def _state(self, set_name: str, model: str) -> tuple[Index | None, str]:
        labelset = self._sets.get(set_name)
        if isinstance(labelset, Refused):
            return None, f"refused: {labelset.reason}"
        index = self._indexes.get((set_name, model))
        if index is not None:
            return index, "loaded"
        return None, self._why.get((set_name, model), "pending: not indexed yet")

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, labelset in self._sets.items():
            if isinstance(labelset, Refused):
                out[name] = {"state": f"refused: {labelset.reason}"}
            else:
                out[name] = {
                    "version": labelset.version,
                    "labels": list(labelset.labels),
                    "models": {model: self._state(name, model)[1] for model in self._order},
                }
        return out

    async def _build(self, service: Any, labelset: LabelSet, min_similarity: float) -> Index:
        texts = [example for examples in labelset.labels.values() for example in examples]
        embedded = await asyncio.wait_for(service.embed(texts), timeout=BUILD_TIMEOUT_S)
        vectors = np.stack([np.asarray(e.vector, dtype=np.float64) for e in embedded])
        return build_index(labelset, vectors, service.model.revision, min_similarity)

    async def sync(self) -> None:
        """Make every usable (set, model) pair have an index that matches the model now loaded. Cheap when nothing changed (it compares a
        revision and a number); called once at start and on the supervisor's tick, so a model that reloads is re-indexed without a request
        having to wait for it. Never raises: a failure is that pair's status line and, for a model that errors, the registry's `failed`
        state with its backoff."""
        async with self._lock:
            for model in self._order:
                service = self._registry.ready_value(model)
                for set_name, labelset in self._sets.items():
                    if isinstance(labelset, Refused):
                        continue
                    key = (set_name, model)
                    min_similarity = labelset.calibration.get(model)
                    if min_similarity is None:
                        self._indexes.pop(key, None)
                        self._why[key] = f"refused: no calibration for {model}"
                        continue
                    if service is None:
                        self._why.setdefault(key, "pending: model not ready")
                        continue
                    have = self._indexes.get(key)
                    if have is not None and have.revision == service.model.revision and have.min_similarity == min_similarity:
                        continue
                    try:
                        self._indexes[key] = await self._build(service, labelset, min_similarity)
                        self._why.pop(key, None)
                    except TimeoutError:
                        self._why[key] = "pending: indexing overran its deadline"
                        logger.warning("label-set indexing overran its deadline", extra={"labelset": set_name, "model": model})
                    except Exception as exc:  # noqa: BLE001 - a build failure is that pair's status and the model's `failed` state, never a crash of the supervisor
                        self._indexes.pop(key, None)
                        self._why[key] = f"pending: indexing failed ({type(exc).__name__})"
                        self._registry.fail(model, f"{type(exc).__name__} during label-set indexing")
                        logger.warning("label-set indexing failed", extra={"labelset": set_name, "model": model, "error_class": type(exc).__name__})

    @staticmethod
    def _none(count: int) -> Answer:
        empty = {"label": None, "nearest": None, "similarity": None, "runner_up": None, "margin": None, "truncated": False}
        return Answer("none", None, None, [{"index": i, **empty} for i in range(count)])

    def labels_of(self, set_name: str) -> tuple[str, ...] | None:
        """The labels of a set that parsed, else None (unknown, or refused: a refused set has no labels to compare a caller's options with)."""
        labelset = self._sets.get(set_name)
        return tuple(labelset.labels) if isinstance(labelset, LabelSet) else None

    def _indexes_for(self, names: list[str], model: str, service: Any) -> dict[str, Index] | None:
        """An index for EVERY named set, each built from the revision of `model` that is loaded now, else None. A model that is missing one
        of them is skipped as a whole: answering a request's questions from two different models would make `model` in the reply a lie."""
        if service is None:
            return None
        found: dict[str, Index] = {}
        for name in names:
            index, _ = self._state(name, model)
            if index is None or index.revision != service.model.revision:
                return None  # not indexed for the model that is loaded now: the next sync() rebuilds, and the next model answers meanwhile
            found[name] = index
        return found

    async def _answer(self, names: list[str], texts: list[str]) -> "_Hit | None":
        """The first model in the chain that holds an index for every set in `names` embeds `texts` ONCE and answers them against each of the
        sets. None when no model can: a set that is unknown or refused (it has no index), every model skipped, failed, slow or non-finite."""
        if not names:
            return None
        for position, model in enumerate(self._order):
            service = self._registry.ready_value(model)
            indexes = self._indexes_for(names, model, service)
            if indexes is None or service is None:
                continue
            try:
                embedded = await asyncio.wait_for(service.embed(texts), timeout=self._deadline_s)
                results = {name: label_results(embedded, index) for name, index in indexes.items()}
            except TimeoutError:
                logger.warning("label model overran its deadline", extra={"model": model, "deadline_ms": self._deadline_s * 1000})
                continue
            except Exception as exc:  # noqa: BLE001 - a model failure is that model's `failed` state and the next one's turn, never a 5xx; the class only is recorded (no text)
                self._registry.fail(model, f"{type(exc).__name__} during labelling")
                logger.warning("label model failed", extra={"model": model, "error_class": type(exc).__name__})
                continue
            return _Hit(ROLES[min(position, 1)], model, embedded, results)
        return None

    def _version(self, set_name: str) -> str | None:
        labelset = self._sets.get(set_name)
        return labelset.version if isinstance(labelset, LabelSet) else None

    async def label(self, set_name: str, texts: list[str]) -> Answer:
        hit = await self._answer([set_name], texts)
        if hit is None:
            return self._none(len(texts))
        return Answer(hit.served_by, hit.model, self._version(set_name), hit.results[set_name])

    async def decide(self, set_names: list[str], text: str) -> "Decided | None":
        """One text against several sets with ONE embedding pass: what `POST /v1/decide` needs. None means nobody could answer."""
        hit = await self._answer(set_names, [text])
        if hit is None:
            return None
        return Decided(
            hit.served_by, hit.model, {name: self._version(name) or "" for name in set_names},
            {name: results[0] for name, results in hit.results.items()}, sum(e.tokens for e in hit.embedded),
        )
