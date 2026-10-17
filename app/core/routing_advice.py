"""Ask the ML service whether a prompt needs a stronger model (specs/014 FR-027, FR-031; GRAPH_PATTERNS pattern 68).

ADVICE, AND NOTHING ELSE. This module sends one thing out (the user's current message) and brings one thing back (a score). It never
raises, it never reads who is asking, and every way it can fail is a value the caller turns into "stay on the local model". The
decision to escalate is the graph's (`route_model`, comparing the score with `ROUTER_THRESHOLD`); the decision about what a turn may
DO is not made by the router at all: approval, tenant scoping and idempotency run the same under either model.

WHAT IS SENT. Exactly `{"task": "route", "texts": [<the user's message>]}`: no tenant, principal, history, retrieved text or tool output.
A prompt is user data and the classifier only needs the prompt; sending less cannot leak more. A test pins the request body.

WHAT COMES BACK is validated, not trusted: a status other than 200, a body that is not the documented shape, a score that is not a finite
number in [0, 1], or a `served_by` this app does not know is an error, not advice. `served_by: "none"` is a normal answer ("no head is
accepted", "both models are down") and is its own outcome, distinct from a failure, because an operator needs to tell "the router is
broken" from "the router has nothing to say yet".

Fresh `httpx.AsyncClient` per call, as `moderation.py` does and for the same reason: a client shared across event loops (the worker runs
turns in more than one) breaks on the loop that did not create it, and a call this small does not need a pool. The deadline is enforced
twice: httpx's per-phase timeouts and a hard wall-clock bound around the whole exchange, because a peer that trickles bytes defeats the
former.
"""
import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import ML_SERVICE_URL, ROUTER_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

Outcome = Literal["advice", "no_advice", "classifier_error", "classifier_timeout"]


@dataclass(frozen=True)
class Advice:
    outcome: Outcome
    score: float | None = None  # the head's probability that the prompt needs a stronger model; set only when outcome == "advice"
    served_by: str = "none"  # "primary" | "fallback" | "none"
    head_version: str | None = None


NO_ADVICE = Advice("no_advice")
_SERVED_BY = ("primary", "fallback")


def _parse(body: object) -> Advice:
    """The documented /classify answer for one text, or `classifier_error` for anything else."""
    error = Advice("classifier_error")
    if not isinstance(body, dict):
        return error
    served_by = body.get("served_by")
    results = body.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return error
    if served_by == "none":
        return NO_ADVICE
    score = results[0].get("score")
    if served_by not in _SERVED_BY or isinstance(score, bool) or not isinstance(score, int | float):
        return error if score is not None else NO_ADVICE
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return error
    version = body.get("head_version")
    return Advice("advice", float(score), str(served_by), version if isinstance(version, str) else None)


async def advise(text: str) -> Advice:
    """Never raises. The failure's class is logged, never the text."""
    try:
        async with asyncio.timeout(ROUTER_TIMEOUT_SECONDS):  # the whole exchange, not each phase of it
            async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT_SECONDS) as client:
                response = await client.post(f"{ML_SERVICE_URL}/classify", json={"task": "route", "texts": [text]})
        if response.status_code != 200:
            logger.warning("routing advice refused", extra={"status": response.status_code})
            return Advice("classifier_error")
        return _parse(response.json())
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("routing advice timed out", extra={"deadline_s": ROUTER_TIMEOUT_SECONDS})
        return Advice("classifier_timeout")
    except Exception as exc:  # noqa: BLE001 - routing is advisory: whatever went wrong, the turn stays on the local model and only the class is logged
        logger.warning("routing advice failed", extra={"error_class": type(exc).__name__})
        return Advice("classifier_error")
