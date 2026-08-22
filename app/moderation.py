"""Real (not hollow) input moderation — screens each turn's input BEFORE
retrieval, the semantic cache, or any LLM spend (GRAPH_PATTERNS.md pattern
25, wired in via `app/graph.py`'s `moderate_input` node, the first node
after `validate_input`'s ctx/empty-input checks).

Deliberately NOT a machine-learning content classifier: a real one is
either a hosted moderation API (which breaks this app's fully-offline
commitment) or a locally-run guard model (a whole additional Ollama pull
and inference cost on the turn's hot path, for a demo). Instead, this is a
real, testable, pattern-based check for (1) known prompt-injection/
jailbreak phrasings and (2) a small, explicit denylist — genuine
detection with real positive and negative cases, honestly scoped as
"catches known patterns," never oversold as "understands intent." A
no-op default here would be worse than no default at all: it would make a
deployment that never actually screens anything look configured.
"""
import re

from app import metrics


class ModerationResult:
    def __init__(self, allowed: bool, reason: str | None = None):
        self.allowed = allowed
        self.reason = reason


# Known jailbreak/prompt-injection phrasings. Deliberately small and
# explicit rather than an attempt at exhaustive coverage — the value here
# is a REAL, testable check, not a claim of completeness against every
# possible phrasing.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"disregard (all |any )?(previous|prior|above) (instructions|rules)",
        r"you are now (DAN|in developer mode|unrestricted)",
        r"reveal your (system prompt|instructions)",
        r"act as if you have no (restrictions|guidelines|filters)",
        r"pretend (you are|to be) .*(with )?no (restrictions|rules|filters)",
    ]
]

# A small, explicit denylist — a real deployment swaps this for a proper
# moderation model/API; this exists so the port isn't a no-op, not as a
# serious content-safety system.
_DENYLIST_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"how to (make|build|synthesize) (a bomb|explosives|nerve gas)",
    ]
]


def screen(text: str) -> ModerationResult:
    """Real check, run before retrieval/spend. A genuine match fails
    closed (`allowed=False`); an unexpected exception in this function's
    OWN logic fails open (allowed, recorded as `outcome="error"`) — the
    same "a failing safety check must not itself crash the turn" posture
    every other degrade-don't-crash boundary in this app already takes,
    while a real hit still gets refused, not smoothed over.
    """
    try:
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                metrics.agent_moderation_total.labels(outcome="blocked_injection").inc()
                return ModerationResult(
                    False, "possible prompt-injection or jailbreak attempt detected"
                )
        for pattern in _DENYLIST_PATTERNS:
            if pattern.search(text):
                metrics.agent_moderation_total.labels(outcome="blocked_denylist").inc()
                return ModerationResult(False, "disallowed content")
        metrics.agent_moderation_total.labels(outcome="allowed").inc()
        return ModerationResult(True)
    except Exception:  # noqa: BLE001
        metrics.agent_moderation_total.labels(outcome="error").inc()
        return ModerationResult(True)
