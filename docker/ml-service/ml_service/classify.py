"""POST /classify: routing advice from a small trained head over a text embedding (specs/014 FR-027..030, contracts/ml-service.md).

ONE TASK, STILL NAMED. `task: "route"` (a head's probability that a prompt needs a stronger model) is all this endpoint does. The topic matcher
that briefly lived here as `task: "label"` (specs/015) is `POST /v1/decide` (decide.py, specs/016): a request that names the old task gets a 400
that says where it went. Nothing in the app calls `/classify` any more; it stays for an accepted head and is the thing to delete if none is
ever accepted (the open question in specs/015). A task is always named by the caller and never guessed from the shape of the body.

WHAT IT IS. The app asks one question per turn: "does this prompt need a stronger model?". This service answers with a probability,
`sigmoid(w . x + b)` over the L2-normalised embedding `x` of the prompt, where `(w, b)` is a head trained offline for ONE embedding
model. The embedder is the same `EmbedService` that serves `/v1/embeddings`, so the classifier's vectors are the served vectors by
construction, and one model loaded for two roles is loaded once.

ADVICE, NEVER AUTHORITY. The score is a number; the threshold that turns it into "escalate" belongs to the caller. A failure of any
kind ends in `served_by: "none"` with HTTP 200, never a 5xx the app would have to interpret: "no advice" is a normal answer and the
app treats it as "stay on the local model" (specs/014 research.md C2).

THREE RULES KEEP A HEAD FROM DOING HARM, and each one is a refusal at load or bind time, not a hope at request time:

1. Only a head whose JSON says `"accepted": true` is served. A head file is written with `false`; a person sets it after the evaluation
   harness shows the head beats the random and prompt-length routers on held-out data (FR-038). A trained-but-unvalidated head
   therefore cannot influence a turn. The test is `is True`, so the string "true" or the number 1 do not pass.
2. A head is bound to the exact model it was trained on: registry name, repo, file, the full commit sha the weights were REALLY read
   from (`TextModel.revision`, taken from the Hub cache path and verified at load; not the declared one), and the dimension. bge-small
   and MiniLM are both 384-d but different spaces; a head over the wrong one returns confident nonsense with no error anywhere.
3. A head file is data, never code: `np.load(..., allow_pickle=False)`, shapes and finiteness checked, so a damaged or hostile file
   is a `refused: malformed` line in `/health/models` and nothing else; a directory the service's user cannot read is `refused: unreadable`
   for each head, and the rest of the service starts and serves as usual.

FAILOVER. The classify-role models in `models.toml` order are the chain: the first is the primary, a later one is the fallback. A model
is skipped when its head is not usable or it is not `ready`. If the one chosen raises, it goes through the registry's `failed` state
(backoff, then the supervisor reloads it and it is the primary again); if it only overruns `ML_CLASSIFY_DEADLINE_MS` the request moves
on but the model stays `ready`: slow once is not broken. The deadline is shorter than the app's own, so the app's timeout is a
backstop and not the usual way a slow primary is noticed.

NO TEXT IS LOGGED. A prompt is user data. Logs carry the model's name and the exception's class; metrics carry `served_by` only.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ml_service.http import error
from ml_service.metrics import Metrics
from ml_service.registry import ModelSpec, Registry
from ml_service.settings import Settings

logger = logging.getLogger("ml_service.classify")

MAX_ITEMS = 16
TASKS = ("route",)
HEAD_SCHEMA_VERSION = 1
ROLES = ("primary", "fallback")


@dataclass(frozen=True)
class Head:
    """A head read from disk and accepted for serving, not yet bound to a loaded model."""

    model: str
    repo: str
    file: str
    revision: str
    dim: int
    w: np.ndarray  # float64 (n_dim,)
    b: float
    version: str
    threshold: float


@dataclass(frozen=True)
class Refused:
    reason: str  # the text after "refused: " in /health/models


def _meta(path: Path) -> dict[str, Any] | Refused:
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return Refused(f"malformed ({type(exc).__name__} reading {path.name})")
    if not isinstance(meta, dict):
        return Refused(f"malformed ({path.name} is not a JSON object)")
    return meta


def _string(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def read_head(directory: Path, model: str) -> Head | Refused | None:
    """The head for `model` in `directory`: `None` when there is none, `Refused` when there is one that must not be served, else the
    `Head`. Never raises: a bad file is that model's `refused` line, not a crash at start-up."""
    json_path, npz_path = directory / f"head-{model}.json", directory / f"head-{model}.npz"
    try:
        has_json, has_npz = json_path.exists(), npz_path.exists()
    except OSError as exc:  # Path.exists() swallows only "not found"-class errors; a directory this user may not search raises PermissionError
        # Found by CI, not by a test: a container running as uid 1000 mounting a 0700 directory. Un-guarded, it escaped the lifespan and the
        # whole service (embeddings, rerank, prompt guard) failed to start for want of a routing file. It is this model's refusal instead.
        return Refused(f"unreadable ({type(exc).__name__} looking for {json_path.name}; the directory must be readable by the service's user)")
    if not has_json and not has_npz:
        return None
    if not has_json or not has_npz:
        return Refused(f"malformed (needs both {json_path.name} and {npz_path.name})")
    meta = _meta(json_path)
    if isinstance(meta, Refused):
        return meta
    if meta.get("accepted") is not True:  # checked first and strictly: this is the line between "trained" and "may advise"
        return Refused("not accepted")
    if meta.get("schema_version") != HEAD_SCHEMA_VERSION:
        return Refused(f"malformed (schema_version {meta.get('schema_version')!r}, this service reads {HEAD_SCHEMA_VERSION})")
    fields = {key: _string(meta, key) for key in ("model", "model_repo", "model_revision", "model_file", "head_version")}
    missing = [key for key, value in fields.items() if value is None]
    dim = meta.get("dim")
    threshold = (meta.get("threshold") or {}).get("value") if isinstance(meta.get("threshold"), dict) else None
    if missing or not isinstance(dim, int) or isinstance(dim, bool) or dim < 1:
        return Refused(f"malformed (missing or invalid {', '.join(missing) or 'dim'})")
    if not isinstance(threshold, int | float) or isinstance(threshold, bool) or not 0.0 <= threshold <= 1.0:
        return Refused("malformed (threshold.value must be a number in [0, 1])")
    try:
        with np.load(npz_path, allow_pickle=False) as arrays:  # a pickle in a mounted file would be code execution
            w, b = np.asarray(arrays["w"]), np.asarray(arrays["b"])
    except Exception as exc:  # noqa: BLE001 - any unreadable file is a refusal for that one head, never a failed start-up
        return Refused(f"malformed ({type(exc).__name__} reading {npz_path.name})")
    if w.dtype.kind != "f" or b.dtype.kind != "f" or w.shape != (dim,) or b.size != 1:
        return Refused(f"malformed (w must be float[{dim}] and b a float scalar)")
    if not (np.isfinite(w).all() and np.isfinite(b).all()):
        return Refused("malformed (non-finite weights)")
    return Head(
        model=fields["model"] or "", repo=fields["model_repo"] or "", file=fields["model_file"] or "",
        revision=fields["model_revision"] or "", dim=dim, w=w.astype(np.float64), b=float(b.reshape(())),
        version=fields["head_version"] or "", threshold=float(threshold),
    )


def read_heads(directory: Path, models: list[str]) -> dict[str, Head | Refused | None]:
    return {name: read_head(directory, name) for name in models}


@dataclass(frozen=True)
class Answer:
    served_by: str  # "primary" | "fallback" | "none"
    model: str | None
    head_version: str | None
    results: list[dict[str, Any]]


class Classifier:
    def __init__(
        self, registry: Registry, specs: list[ModelSpec], heads: dict[str, Head | Refused | None], settings: Settings, metrics: Metrics
    ) -> None:
        self._registry = registry
        self._order = [s.name for s in specs if "classify" in s.roles]
        self._heads = heads
        self._deadline_s = settings.classify_deadline_ms / 1000
        self._metrics = metrics

    def _bind(self, name: str) -> tuple[Head | None, str]:
        """The head to use for `name` now, and the status line `/health/models` shows. Evaluated per call: it is four comparisons, and a
        model that reloads (failed, then ready) is checked against the revision it NOW holds."""
        head = self._heads.get(name)
        if head is None:
            return None, "absent"
        if isinstance(head, Refused):
            return None, f"refused: {head.reason}"
        service = self._registry.ready_value(name)
        if service is None:
            return None, "pending: model not ready"
        loaded = service.model
        for label, same in (
            ("model", head.model == name),
            ("repo", head.repo == loaded.spec.repo),
            ("file", head.file == loaded.spec.file),
            ("revision", head.revision == loaded.revision),  # what the cache really held, not what the registry asked for
            ("dimension", head.dim == loaded.dim),
        ):
            if not same:
                return None, f"refused: {label} mismatch"
        return head, "loaded"

    def serving(self) -> str:
        """Who would answer right now: the first model in the chain with a usable head."""
        for index, name in enumerate(self._order):
            if self._bind(name)[0] is not None:
                return ROLES[min(index, 1)]
        return "none"

    def snapshot(self) -> dict[str, Any]:
        return {"serving": self.serving(), "heads": {name: self._bind(name)[1] for name in self._order}}

    def publish(self) -> None:
        """Set the serving gauge from the CURRENT state. Called when metrics are scraped, not per request: an alert on 'the fallback has
        been serving for 15 minutes' must be right during a quiet hour too, when no request would have refreshed a per-request gauge."""
        serving = self.serving()
        for role in ROLES:
            self._metrics.classifier_serving.labels(served_by=role).set(1 if serving == role else 0)

    @staticmethod
    def _none(count: int) -> Answer:
        return Answer("none", None, None, [{"index": i, "decision": "none", "score": None, "truncated": False} for i in range(count)])

    async def classify(self, texts: list[str]) -> Answer:
        for index, name in enumerate(self._order):
            head, _ = self._bind(name)
            service = self._registry.ready_value(name)
            if head is None or service is None:
                continue
            try:
                embedded = await asyncio.wait_for(service.embed(texts), timeout=self._deadline_s)
                answer = self._score(embedded, head, ROLES[min(index, 1)])
            except TimeoutError:
                # Slow, not broken: the model stays `ready` and the next request tries it again.
                logger.warning("classifier model overran its deadline", extra={"model": name, "deadline_ms": self._deadline_s * 1000})
                continue
            except Exception as exc:  # noqa: BLE001 - a model failure is that model's `failed` state and the next one's turn, never a 5xx; the class only is recorded (no text)
                self._registry.fail(name, f"{type(exc).__name__} during classification")
                logger.warning("classifier model failed", extra={"model": name, "error_class": type(exc).__name__})
                continue
            return answer
        return self._none(len(texts))

    def _score(self, embedded: list[Any], head: Head, served_by: str) -> Answer:
        vectors = np.stack([np.asarray(e.vector, dtype=np.float64) for e in embedded])
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        unit = vectors / np.where(norms == 0.0, 1.0, norms)
        logits = unit @ head.w + head.b
        scores = 0.5 * (1.0 + np.tanh(logits / 2.0))  # sigmoid, without exp overflow for a large |logit|
        if not np.isfinite(scores).all():
            raise ValueError("non-finite score")  # a glitching model must reach the fallback, and JSON cannot carry NaN
        results = [
            {
                "index": i,
                "decision": "needs_stronger" if score >= head.threshold else "local_ok",
                "score": float(score),
                "truncated": bool(e.truncated),
            }
            for i, (score, e) in enumerate(zip(scores, embedded, strict=True))
        ]
        return Answer(served_by, head.model, head.version, results)


class _Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message


@dataclass(frozen=True)
class Parsed:
    task: str  # "route"
    texts: list[str]


def _validate(body: Any, settings: Settings) -> Parsed:
    if not isinstance(body, dict):
        raise _Reject("invalid_input", "the request body must be a JSON object")
    task = body.get("task")
    if task == "label":
        raise _Reject("invalid_task", 'task "label" moved: ask a `choice` question at POST /v1/decide (the question is named after the label set)')
    if task not in TASKS:
        raise _Reject("invalid_task", '`task` is required and must be "route"')  # a task is named, never inferred from the shape of the request
    texts = body.get("texts")
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) and t for t in texts):
        raise _Reject("invalid_input", "`texts` must be a non-empty list of non-empty strings")
    if len(texts) > MAX_ITEMS:
        raise _Reject("batch_too_large", f"{len(texts)} texts exceeds the limit of {MAX_ITEMS}")
    for index, text in enumerate(texts):
        if len(text) > settings.classify_max_chars:
            raise _Reject("input_too_long", f"text {index} has {len(text)} characters; the limit is {settings.classify_max_chars}")
    return Parsed(task, texts)


router = APIRouter()


@router.post("/classify", response_model=None)
async def classify(request: Request):
    app = request.app
    try:
        body = await request.json()
    except ValueError:
        return error(400, "invalid_input", "the request body is not valid JSON")
    try:
        parsed = _validate(body, app.state.settings)
    except _Reject as rejected:
        return error(400, rejected.code, rejected.message)
    started = time.perf_counter()
    answer = await app.state.classifier.classify(parsed.texts)
    app.state.metrics.classify_requests.labels(served_by=answer.served_by).inc()
    logger.debug("classified", extra={"served_by": answer.served_by, "items": len(parsed.texts), "ms": round((time.perf_counter() - started) * 1000, 1)})
    return JSONResponse(
        {"served_by": answer.served_by, "model": answer.model, "head_version": answer.head_version, "results": answer.results}
    )

