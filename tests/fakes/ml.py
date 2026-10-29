"""Fakes for the ports in `app/ports/classification.py` and `app/ports/screening.py` (the ML service)."""
from collections.abc import Awaitable, Callable

from app.ports.classification import NO_ANSWER, Choice, Labelled


class NoAnswerClassifier:
    """A classifier with nothing to say: what the real adapter returns for an unreachable ML service, so the router decides without it."""

    async def choose(self, question: Choice, text: str) -> Labelled:
        return NO_ANSWER


class ScriptedClassifier:
    """Answers each question with `answer(question, text)`, and remembers what it was asked."""

    def __init__(self, answer: Callable[[Choice, str], Awaitable[Labelled]]) -> None:
        self._answer = answer
        self.asked: list[tuple[Choice, str]] = []

    async def choose(self, question: Choice, text: str) -> Labelled:
        self.asked.append((question, text))
        return await self._answer(question, text)


class ConstantScorer:
    """An injection scorer that gives every text the same score (0.0, by default: nothing is malicious)."""

    def __init__(self, score: float = 0.0) -> None:
        self.score = score

    async def malicious_score(self, text: str) -> float:
        return self.score


def use_classifier(monkeypatch, choose) -> None:
    """Binds a classifier whose `choose(question, text)` is the given coroutine function, for the duration of the test."""
    classifier = type("_Scripted", (), {"choose": staticmethod(choose)})()
    from app.ports.classification import route_classifier

    monkeypatch.setattr(route_classifier, "get", lambda: classifier)
