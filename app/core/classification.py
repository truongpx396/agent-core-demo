"""Which of a few predefined choices is a text nearest to? The app's client for the ML service's label sets (specs/015, contracts/label-sets.md).

THE SERVICE ANSWERS WITH FACTS; THE CALLER DECIDES. This module sends one text and a set's name, and brings back the nearest label (or `None`
when the text is near none of them), how near, and how clearly it beat the runner-up. What a label MEANS (a model tier, an email, a queue) is the
caller's decision: `routing_policy.LABEL_TIERS` for the model router, and whatever table the next agent step writes for its own set.

IT NEVER RAISES, AND A FAILURE IS A VALUE. A service that is down, slow, wrong-shaped or has nothing to say is an `outcome` the caller turns into
"decide without a label". `served_by: "none"` is a normal answer ("no model has indexed this set yet", "both models are down") and is its own
outcome, distinct from a failure, because an operator needs to tell "the classifier is broken" from "the classifier has nothing to say".

WHAT IS SENT. The set's name and the first `MAX_CHARS` characters of the text: a message's topic is evident from its opening, and the service
would otherwise reject a long paste outright. Not the tenant, the principal, the history or any retrieved text: the classifier only needs the
text, and sending less cannot leak more (specs/014 FR-031). A test pins the request body.

WHAT COMES BACK is validated, not trusted: a status other than 200, a body that is not the documented shape, a set name or label that is not a
string, a similarity that is not a finite number in [-1, 1], or a `served_by` this app does not know is an error, not an answer.

A fresh `httpx.AsyncClient` per call, as `moderation.py` does and for the same reason: a client shared across event loops (the worker runs turns
in more than one) breaks on the loop that did not create it, and a call this small does not need a pool. The deadline is enforced twice:
httpx's per-phase timeouts and a hard wall-clock bound around the whole exchange, because a peer that trickles bytes defeats the former.
"""
import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import ML_SERVICE_URL, ROUTER_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

MAX_CHARS = 2000
Outcome = Literal["labelled", "no_answer", "timeout", "error"]
_SERVED_BY = ("primary", "fallback")


@dataclass(frozen=True)
class Labelled:
    outcome: Outcome  # "labelled": the service answered (label may still be None); "no_answer": served_by none; "timeout"; "error"
    label: str | None = None  # the nearest label if it cleared the serving model's cutoff, else None
    nearest: str | None = None  # the nearest label either way
    similarity: float | None = None
    margin: float | None = None
    served_by: str = "none"
    labelset_version: str | None = None


NO_ANSWER = Labelled("no_answer")


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return float(value)


def _parse(body: object, labelset: str) -> Labelled:
    """The documented answer for one text, or `error` for anything else."""
    error = Labelled("error")
    if not isinstance(body, dict) or body.get("labelset") not in (labelset, None):
        return error
    served_by = body.get("served_by")
    results = body.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return error
    if served_by == "none":
        return NO_ANSWER
    if served_by not in _SERVED_BY:
        return error
    result = results[0]
    label, nearest = result.get("label"), result.get("nearest")
    similarity, margin = _number(result.get("similarity"), -1.0, 1.0), _number(result.get("margin"), 0.0, 2.0)
    if similarity is None or margin is None or not isinstance(nearest, str) or not (label is None or isinstance(label, str)):
        return error
    version = body.get("labelset_version")
    return Labelled("labelled", label, nearest, similarity, margin, str(served_by), version if isinstance(version, str) else None)


async def label(labelset: str, text: str) -> Labelled:
    """Never raises. The failure's class is logged, never the text."""
    try:
        async with asyncio.timeout(ROUTER_TIMEOUT_SECONDS):  # the whole exchange, not each phase of it
            async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{ML_SERVICE_URL}/classify", json={"task": "label", "labelset": labelset, "texts": [text[:MAX_CHARS]]}
                )
        if response.status_code != 200:
            logger.warning("label request refused", extra={"status": response.status_code, "labelset": labelset})
            return Labelled("error")
        return _parse(response.json(), labelset)
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("label request timed out", extra={"deadline_s": ROUTER_TIMEOUT_SECONDS, "labelset": labelset})
        return Labelled("timeout")
    except Exception as exc:  # noqa: BLE001 - classification is advisory: whatever went wrong, the caller decides without a label and only the class is logged
        logger.warning("label request failed", extra={"error_class": type(exc).__name__, "labelset": labelset})
        return Labelled("error")
