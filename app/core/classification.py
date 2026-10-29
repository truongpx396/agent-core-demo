"""Ask a typed question about a text and get one of a few predefined answers: the app's client for `POST
/v1/decide` (specs/016, contracts/decide.md).

THE INTERFACE IS NOT OURS, ON PURPOSE. `/v1/decide` takes and returns the shape of the TypeSafe "System One" /
Jev decision interface: `{state, questions}` in, `{model, answers, usage}` out, with `choice` questions whose
`criteria` map each option to a description. The ML service is one (local) implementation; a hosted one is
another. This is the only module that knows the wire, so pointing the app at the hosted one is a change here
(the URL, an `Authorization` header, a `model` field) and in nothing that calls it. `Choice` carries what the
hosted one needs and the local one ignores (the instructions and a description of every option), so a caller
written once works on both.

THE SERVICE ANSWERS WITH FACTS; THE CALLER DECIDES. What comes back is the chosen option (or `None`: the local
implementation abstains when the text is near none of them) and, when offered, how near it was and how sure.
What an option MEANS (a model tier, an email, a queue) is the caller's: `routing_policy.LABEL_TIERS` for the
model router, and whatever table the next agent step writes.

IT NEVER RAISES, AND A FAILURE IS A VALUE. A service that is down, slow, wrong-shaped or has nothing to say is
an `outcome` the caller turns into "decide without an answer". 503 `model_unavailable` ("no model has indexed
this set yet", "both models are down") is its own outcome, because an operator must tell "the classifier is
broken" from "the classifier has nothing to say".

WHAT IS SENT. The question (name, instructions, options) and the first `MAX_CHARS` characters of the text: a
message's topic is evident from its opening, and the service would reject a long paste outright. Not the
tenant, principal, history or retrieved text: the classifier only needs the text, and sending less can't leak
more (specs/014 FR-031). A test pins the request body, and it is also the whole of what a hosted
implementation would receive.

WHAT COMES BACK is validated, not trusted: a status other than 200, a body not in the documented shape, an
answer that isn't a `choice` for the question asked, a `choice` outside the options, a similarity or
confidence that isn't a finite in-range number, or an `x_served_by` this app doesn't know is an error, not an
answer.

A fresh `httpx.AsyncClient` per call, as `moderation.py` does and for the same reason: a client shared across
event loops (the worker runs turns in more than one) breaks on the loop that didn't create it, and a call this
small needs no pool. The deadline is enforced twice, by httpx's per-phase timeouts and by a hard wall-clock
bound around the whole exchange, because a peer that trickles bytes defeats the former.
"""
import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import ML_SERVICE_URL, ROUTER_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

MAX_CHARS = 2000
DECIDE_PATH = "/v1/decide"
Outcome = Literal["labelled", "no_answer", "timeout", "error"]
_SERVED_BY = ("primary", "fallback")


@dataclass(frozen=True)
class Choice:
    """A `choice` question: pick one of `options`. `name` is also the name of the ML service's label set that answers it, and the service refuses
    a question whose option names are not exactly that set's labels, so a question and its set cannot drift apart unnoticed."""

    name: str
    instructions: str
    options: Mapping[str, str]  # option -> what it means; the local implementation ignores the words, a hosted one reads them


@dataclass(frozen=True)
class Labelled:
    outcome: Outcome  # "labelled": the service answered (the choice may still be None); "no_answer": nothing could; "timeout"; "error"
    label: str | None = None  # the option chosen, or None when the implementation found the text near none of them
    nearest: str | None = None  # the nearest option either way (the local implementation reports it; a hosted one does not)
    similarity: float | None = None
    margin: float | None = None
    served_by: str = "none"
    labelset_version: str | None = None
    confidence: float | None = None  # a calibrated 0..1 where the implementation has one (a hosted one does; the local one does not)


NO_ANSWER = Labelled("no_answer")


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return float(value)


def _optional(value: object, low: float, high: float) -> tuple[bool, float | None]:
    """(acceptable, the number): absent is acceptable and None; present must be a finite number in range."""
    if value is None:
        return True, None
    number = _number(value, low, high)
    return number is not None, number


def _parse(body: object, question: Choice) -> Labelled:
    """The documented answer to this one question, or `error` for anything else."""
    error = Labelled("error")
    if not isinstance(body, dict):
        return error
    answers = body.get("answers")
    answer = answers.get(question.name) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return error
    choice = answer.get("choice")
    if not (choice is None or (isinstance(choice, str) and choice in question.options)):
        return error
    evidence = answer.get("x_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    similarity_ok, similarity = _optional(evidence.get("similarity"), -1.0, 1.0)
    margin_ok, margin = _optional(evidence.get("margin"), 0.0, 2.0)
    confidence_ok, confidence = _optional(answer.get("confidence"), 0.0, 1.0)
    served_by = body.get("x_served_by")
    if not (similarity_ok and margin_ok and confidence_ok) or (served_by is not None and served_by not in _SERVED_BY):
        return error  # a number was offered and is not a number in range: a wrong-shaped answer is not partly trusted
    nearest, version = evidence.get("nearest"), answer.get("x_labelset_version")
    return Labelled(
        "labelled", choice, nearest if isinstance(nearest, str) else None, similarity, margin,
        str(served_by) if served_by else "remote", version if isinstance(version, str) else None, confidence,
    )


def _unavailable(response: httpx.Response) -> bool:
    """503 with the interface's own code for 'cannot answer now'. Anything else that is not 200 is a failure, not a quiet no."""
    if response.status_code != 503:
        return False
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return False
    return isinstance(error, dict) and error.get("code") == "model_unavailable"


async def choose(question: Choice, text: str) -> Labelled:
    """Never raises. The failure's class is logged, never the text."""
    request = {
        "state": text[:MAX_CHARS],
        "questions": {question.name: {"type": "choice", "instructions": question.instructions, "criteria": dict(question.options)}},
    }
    try:
        async with asyncio.timeout(ROUTER_TIMEOUT_SECONDS):  # the whole exchange, not each phase of it
            async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT_SECONDS) as client:
                response = await client.post(f"{ML_SERVICE_URL}{DECIDE_PATH}", json=request)
        if _unavailable(response):
            return NO_ANSWER
        if response.status_code != 200:
            logger.warning("decision request refused", extra={"status": response.status_code, "question": question.name})
            return Labelled("error")
        return _parse(response.json(), question)
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("decision request timed out", extra={"deadline_s": ROUTER_TIMEOUT_SECONDS, "question": question.name})
        return Labelled("timeout")
    except Exception as exc:  # noqa: BLE001 - classification is advisory: whatever went wrong, the caller decides without an answer and only the class is logged
        logger.warning("decision request failed", extra={"error_class": type(exc).__name__, "question": question.name})
        return Labelled("error")
