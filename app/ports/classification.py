"""Ask a typed question about a text and get one of a few predefined answers: the router's port to a classifier.

The caller gives a `Choice` (a question and its options) and a text; the answer is a `Labelled`: the option chosen, or None when the classifier
found the text near none of them, and, when offered, how near and how sure. What an option MEANS (a model tier, a queue) is the caller's
(`app/core/routing_policy.py::LABEL_TIERS` for the router).

The shape follows the TypeSafe "System One" / Jev decision interface (`{state, questions}` in, `{model, answers, usage}` out), on purpose: the
ML service is one local implementation, a hosted one another, and pointing the app at the hosted one changes only the adapter
(`app/adapters/mlservice/classification.py`: URL, `Authorization` header, `model` field). `Choice` carries what the hosted one needs and the
local one ignores (instructions, option descriptions).

`choose` NEVER RAISES; a failure is a value. A classifier that is down, slow, wrong-shaped or has nothing to say is an `outcome` the caller turns
into "decide without an answer".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from app.ports.slot import Slot

Outcome = Literal["labelled", "no_answer", "timeout", "error"]


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


NO_ANSWER = Labelled("no_answer")


class RouteClassifier(Protocol):
    async def choose(self, question: Choice, text: str) -> Labelled:
        """Never raises. Sends the question and a bounded prefix of the text, and nothing about the tenant, person, history or retrieved text."""
        ...


route_classifier: Slot[RouteClassifier] = Slot("route_classifier", RouteClassifier)
