"""The prompt-injection scorer behind input moderation.

`app/core/moderation.py` decides: a regex layer first, then this score against a threshold, failing closed on a real hit and open on a failure of
the check itself. This port is only the score (`app/adapters/mlservice/prompt_guard.py`: Meta's Llama Prompt Guard 2, 22M, served by the same
`ml-service` container the reranker uses).
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class InjectionScorer(Protocol):
    async def malicious_score(self, text: str) -> float:
        """The probability, 0..1, that `text` is an attempt to override the agent's instructions. Raises on any failure to get an answer:
        the caller owns the fail-open decision."""
        ...


injection_scorer: Slot[InjectionScorer] = Slot("injection_scorer", InjectionScorer)
