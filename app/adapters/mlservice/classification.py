"""The `RouteClassifier` adapter (`app/ports/classification.py`): the app's client for `POST
/v1/decide` (specs/016, contracts/decide.md).

The interface is not ours, on purpose: `/v1/decide` takes and returns the shape of the TypeSafe "System One" /
Jev decision interface (`{state, questions}` in, `{model, answers, usage}` out, `choice` questions whose
`criteria` map each option to a description). The ML service is one local implementation, a hosted one
another. This is the only module that knows the wire, so pointing the app at the hosted one changes only this
(URL, `Authorization` header, `model` field). `Choice` carries what the hosted one needs and the local one
ignores (instructions, option descriptions).

The service answers with facts; the caller decides. The result is the chosen option (or `None`: the local
service abstains when the text is near none) and, when offered, how near and how sure. What an option MEANS (a
model tier, a queue) is the caller's (`routing_policy.LABEL_TIERS` for the router).

It never raises; a failure is a value. A service that is down, slow, wrong-shaped or has nothing to say is an
`outcome` the caller turns into "decide without an answer". 503 `model_unavailable` is its own outcome, so an
operator can tell "the classifier is broken" from "it has nothing to say".

What is sent: the question and the first `MAX_CHARS` characters of the text (a topic is evident from the
opening, and a long paste would be rejected). Not the tenant, principal, history or retrieved text (specs/014
FR-031); a test pins the request body.

What comes back is validated, not trusted: a non-200, a body off the documented shape, an answer that isn't a
`choice` for the question asked, a `choice` outside the options, a non-finite or out-of-range similarity or
confidence, or an unknown `x_served_by` is an error.

A fresh `httpx.AsyncClient` per call, as `moderation.py` does: a client shared across event loops (the worker
has several) breaks on the loop that didn't create it. The deadline is enforced twice, by httpx's phase
timeouts and a wall-clock bound around the exchange, because a peer that trickles bytes defeats the former.
"""
import asyncio
import logging
import math

import httpx

from app.core.config import ML_SERVICE_URL, ROUTER_TIMEOUT_SECONDS
from app.ports.classification import NO_ANSWER, Choice, Labelled

logger = logging.getLogger(__name__)

MAX_CHARS = 2000
DECIDE_PATH = "/v1/decide"
_SERVED_BY = ("primary", "fallback")


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


class MlServiceClassifier:
    """The adapter: `choose` behind the port, looked up at CALL time so a test that patches the module function is seen."""

    async def choose(self, question: Choice, text: str) -> Labelled:
        return await choose(question, text)
