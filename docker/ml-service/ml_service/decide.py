"""POST /v1/decide: typed decisions, in the request and answer shape of the TypeSafe "System One" / Jev interface (specs/016, contracts/decide.md).

WHY THIS SHAPE. A decision (which of these topics is this? should a human take over?) is a different thing from generated text, and a hosted
service that answers it already exists with a documented wire format: `{model, state, questions}` in, `{model, answers, usage}` out. The app
asks its questions in that shape, so pointing it at another implementation of the same interface is a change of URL, key and model name in the
app's client (app/core/classification.py) and no change to any caller. This service is one implementation, a local one: no text leaves the
machine, no per-call cost, a few milliseconds. It is NOT a copy of the hosted one and does not pretend to be (see "WHAT DIFFERS").

WHAT IS ANSWERED. A `choice` question: `criteria` maps each option to a description, the answer is one of the options. Here the question's NAME
is the name of a label set (labelsets.py), because the matcher learns from the set's example sentences and the interface has no place for
examples; the `criteria` keys must be exactly the set's labels, so a caller and a set that drifted apart is a 400 and not a quietly wrong answer.
Several questions in one request are answered by one model with one embedding pass.

WHAT DIFFERS from the hosted interface, each one deliberate and listed in the contract:
- `choice` is `null` when the text is near none of the options (the set's cutoff, per model). The hosted model always names an option; a caller
  that wants "none of these" there adds an option for it. A caller of this service must handle `null`.
- No `confidence` and no `probabilities`. A cosine similarity is not a probability, and a number that looks like one invites a threshold that
  means nothing. The evidence is in `x_evidence` (nearest label, similarity, runner-up, margin) under its own name.
- `state` is a string (the hosted one also takes an object or an array); `instructions` and the option descriptions are required for the shape
  and not used for matching; only `choice` is implemented, `score` and `noul` are a 422, and `assets` is a 422.
- `model` is optional and can only be "local". No authentication: this is an internal service.
- `usage.input_tokens` is the tokens the embedder was fed; `output_tokens` is 0.
- A request nothing can answer is 503 `model_unavailable` with `Retry-After`, as the hosted interface says "temporarily overloaded".
Fields this service adds are prefixed `x_`, so a reader of the documented fields never meets them by accident.

NO TEXT IS LOGGED. `state` is user data. Logs carry the decisions' names, the serving model and an exception's class; metrics carry the
decision's name (one the service already knows) and `served_by`.
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ml_service.http import error
from ml_service.labelsets import Labeller
from ml_service.settings import Settings

logger = logging.getLogger("ml_service.decide")

MAX_QUESTIONS = 8
MAX_OPTIONS = 255  # the hosted interface's own limit on the options of one question
LOCAL_MODEL = "local"
QUESTION_TYPES = ("choice", "score", "noul")
IMPLEMENTED = ("choice",)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUEST_KEYS = frozenset({"model", "state", "questions"})
QUESTION_KEYS = frozenset({"type", "instructions", "criteria"})
RETRY_AFTER_S = "1"  # an index that is still being built is ready within a supervisor tick


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Parsed:
    state: str
    names: tuple[str, ...]  # the questions' names, in request order; each is a label set


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_question(name: Any, question: Any) -> dict[str, str]:
    """The question's options (option -> description) if it is a well-formed `choice` question; a _Reject for anything else."""
    if not isinstance(name, str) or not NAME.match(name):
        raise _Reject(400, "invalid_input", f"a question name must match {NAME.pattern}")
    if not isinstance(question, dict):
        raise _Reject(400, "invalid_input", f"question {name!r} must be an object")
    unknown = sorted(set(question) - QUESTION_KEYS)
    if unknown:
        raise _Reject(400, "invalid_input", f"question {name!r} has fields this interface does not define: {', '.join(map(str, unknown))}")
    kind = question.get("type")
    if kind not in QUESTION_TYPES:
        raise _Reject(400, "invalid_input", f"question {name!r}: `type` must be one of {', '.join(QUESTION_TYPES)}")
    if kind not in IMPLEMENTED:
        raise _Reject(422, "unsupported_question_type", f"question {name!r}: this service answers {', '.join(IMPLEMENTED)} questions, not {kind!r}")
    if not _text(question.get("instructions")):
        raise _Reject(400, "invalid_input", f"question {name!r}: `instructions` must be a non-empty string")
    criteria = question.get("criteria")
    if not isinstance(criteria, dict) or not 1 <= len(criteria) <= MAX_OPTIONS:
        raise _Reject(400, "invalid_input", f"question {name!r}: `criteria` must be an object of 1..{MAX_OPTIONS} options")
    if not all(isinstance(option, str) and option and isinstance(description, str) for option, description in criteria.items()):
        raise _Reject(400, "invalid_input", f"question {name!r}: every option needs a name and a string description")
    return dict(criteria)


def _validate(body: Any, settings: Settings, labeller: Labeller) -> Parsed:
    if not isinstance(body, dict):
        raise _Reject(400, "invalid_input", "the request body must be a JSON object")
    if "assets" in body:
        raise _Reject(422, "unsupported_field", "`assets` (images) are not supported by this service")
    unknown = sorted(set(body) - REQUEST_KEYS)
    if unknown:
        raise _Reject(400, "invalid_input", f"the request has fields this interface does not define: {', '.join(map(str, unknown))}")
    if "model" in body and body["model"] != LOCAL_MODEL:
        raise _Reject(400, "unknown_model", f'`model` can only be "{LOCAL_MODEL}" (or left out) on this service')
    state = body.get("state")
    if isinstance(state, dict | list):
        raise _Reject(422, "unsupported_state", "`state` must be a string on this service (an object or an array is not supported)")
    if not isinstance(state, str) or not state.strip():  # (not _text(): mypy cannot narrow through a helper)
        raise _Reject(400, "invalid_input", "`state` must be a non-empty string")
    if len(state) > settings.classify_max_chars:
        raise _Reject(413, "input_too_long", f"`state` has {len(state)} characters; the limit is {settings.classify_max_chars}")
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise _Reject(400, "invalid_input", "`questions` must be an object with at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise _Reject(400, "too_many_questions", f"{len(questions)} questions exceeds the limit of {MAX_QUESTIONS}")
    options = {name: _check_question(name, question) for name, question in questions.items()}
    for name, given in options.items():
        if not labeller.knows(name):
            raise _Reject(400, "unknown_decision", f"no decision named {name!r}; a question is named after a label set, and GET /health/models lists them")
        labels = labeller.labels_of(name)
        if labels is not None and set(given) != set(labels):
            missing, extra = sorted(set(labels) - set(given)), sorted(set(given) - set(labels))
            raise _Reject(
                400, "criteria_mismatch",
                f"question {name!r}: `criteria` must name exactly the set's options; missing {missing or 'none'}, not in the set {extra or 'none'}",
            )
    return Parsed(state, tuple(options))


router = APIRouter()


@router.post("/v1/decide", response_model=None)
async def decide(request: Request):
    app = request.app
    try:
        body = await request.json()
    except ValueError:
        return error(400, "invalid_input", "the request body is not valid JSON")
    try:
        parsed = _validate(body, app.state.settings, app.state.labeller)
    except _Reject as rejected:
        return error(rejected.status, rejected.code, rejected.message)
    started = time.perf_counter()
    decided = await app.state.labeller.decide(list(parsed.names), parsed.state)
    served_by = decided.served_by if decided is not None else "none"
    for name in parsed.names:
        app.state.metrics.label_requests.labels(labelset=name, served_by=served_by).inc()
    logger.debug("decided", extra={"decisions": list(parsed.names), "served_by": served_by, "ms": round((time.perf_counter() - started) * 1000, 1)})
    if decided is None:
        return error(503, "model_unavailable", "no model can answer these questions right now", {"Retry-After": RETRY_AFTER_S})
    answers = {
        name: {
            "type": "choice",
            "choice": decided.results[name]["label"],
            "x_evidence": {
                key: decided.results[name][key] for key in ("nearest", "similarity", "runner_up", "margin", "truncated")
            },
            "x_labelset_version": decided.versions[name],
        }
        for name in parsed.names
    }
    return JSONResponse(
        {"model": decided.model, "answers": answers, "usage": {"input_tokens": decided.tokens, "output_tokens": 0}, "x_served_by": decided.served_by}
    )
