"""Which model tier a message should be routed to: two signals, one combining rule, and nothing else (specs/015, contracts/tiered-router.md).

PURE, ON PURPOSE. This module imports nothing from the app and does no I/O, so the graph node that routes a turn and the harness that scores a
router (`make model-eval`, `scripts/model_eval/runners/classify.py` via `run(backend=...)`) run THE SAME `decide`. A router that is evaluated by a
copy of its logic is evaluated by something else.

TWO SIGNALS, EACH NAMING THE LOWEST TIER IT WILL ACCEPT.
  * Rules over the user's current message: its length, whether it contains code, whether it asks several things, whether it uses the words people
    use when they want reasoning. Free, instant, deterministic, and they cannot fail.
  * A label from the ML service's label set (`route-topics`): which kind of work the message asks of a model, and a table here (`LABEL_TIERS`)
    says what tier that kind of work needs. It can be missing (the service is down, slow, or found the message near no topic); then the rules decide.

`tier = the highest configured tier at or below min(ceiling, max(rule tier, label tier))`. `max` is the cost-safe reading of "either signal can
ask for more"; `min` with the operator's ceiling bounds spend; a signal that is missing contributes `local`, so it never cancels the other one.

WHAT THE LABEL TABLE AND THE RULES ARE. Policy, written by a person, with the reasons next to each line. They describe the kind of work a model must
do, not how hard a message is, and nothing measured says they predict where a stronger model answers better: the only routing outcomes that exist
could not tell a perfect router from a random one (spec 014; `specs/015.../research.md`). That is why the router ships in `shadow` mode first. Change
a line knowingly, and re-run the evaluation.
"""
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

Tier = Literal["local", "medium", "strong"]
TIERS: tuple[Tier, ...] = ("local", "medium", "strong")  # cheapest first; the index is the rank
RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(TIERS)}
DecidedBy = Literal["none", "rules", "label", "both"]
LimitedBy = Literal["ceiling", "unconfigured", "unpriced"]

DEFAULT_LONG_PROMPT_CHARS = 1500  # Settings.router_long_prompt_chars defaults to it, and the evaluation harness uses it, so both judge the same router
RULE_IDS = ("long_prompt", "code", "multi_part", "reasoning_markers")  # a closed list: these are the only rule names a log or a test sees

# What kind of work a message asks of a model -> the lowest tier that should do it. Keys must equal the labels of docker/ml-service/labelsets/route-topics.json
# (tests/core/test_routing_policy.py). A small local model is good at the first four: finding a fact, passing a request to a tool (every write is
# approved by a person anyway), a courteous reply, a short draft. Analysis across several considerations and writing or fixing code are where the
# gap to a larger model is most often visible, so they ask for the middle tier. Multi-step calculation and logic, and questions whose answer a
# person may act on in law, money, safety or people management, ask for the strongest: a wrong answer there costs more than the price difference.
LABEL_TIERS: dict[str, Tier] = {
    "smalltalk": "local",
    "lookup": "local",
    "action": "local",
    "drafting": "local",
    "analysis": "medium",
    "code": "medium",
    "reasoning": "strong",
    "high_stakes": "strong",
}

# The words people use when they want a model to reason rather than recall. Two distinct ones ask for the middle tier, three for the strongest
# (LiteLLM's complexity router treats two as the top tier; here a single common word like "compare" is not enough on its own, because "compare the
# plans" is an ordinary support question). Word-bounded, so "improve" does not contain "prove".
_REASONING = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bstep[- ]by[- ]step\b",
        r"\bprove\b|\bproof\b",
        r"\bderive\b|\bderivation\b",
        r"\btrade-?offs?\b",
        r"\broot cause\b",
        r"\boptimi[sz](?:e|ation)\b",
        r"\b(?:think|reason|work)(?: it| this| that)? through\b",
        r"\bpros and cons\b",
        r"\bfirst principles\b",
        r"\bjustify\b",
        r"\bedge cases?\b",
        r"\bcompare and contrast\b",
    )
)
_FENCE = "```"
_TRACEBACK = re.compile(r"(?m)^Traceback \(most recent call last\)")
_INDENTED_CODE = re.compile(r"(?m)^(?: {4}|\t)\S")
_ENUMERATED = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-*•])\s+\S")
# The pattern rules read at most this much of a message. A pasted megabyte must not cost more than a long question; its length is still known.
MAX_SCAN_CHARS = 20_000


@dataclass(frozen=True)
class Rules:
    tier: Tier
    fired: tuple[str, ...]


def evaluate_rules(text: str, *, long_prompt_chars: int) -> Rules:
    """Every rule that fires on `text`, and the highest tier any of them asks for. Never raises."""
    scanned = text[:MAX_SCAN_CHARS]
    asked: dict[str, Tier] = {}
    if len(text) >= long_prompt_chars:
        asked["long_prompt"] = "medium"
    if _FENCE in scanned or _TRACEBACK.search(scanned) or len(_INDENTED_CODE.findall(scanned)) >= 3:
        asked["code"] = "medium"
    if scanned.count("?") >= 3 or len(_ENUMERATED.findall(scanned)) >= 3:
        asked["multi_part"] = "medium"
    lowered = scanned.lower()
    markers = sum(1 for pattern in _REASONING if pattern.search(lowered))
    if markers >= 2:
        asked["reasoning_markers"] = "strong" if markers >= 3 else "medium"
    fired = tuple(asked)  # insertion order is evaluation order is RULE_IDS order (a test pins it)
    tier: Tier = max(asked.values(), key=RANK.__getitem__, default="local")
    return Rules(tier, fired)


@dataclass(frozen=True)
class Decision:
    tier: Tier  # what the turn would use
    asked: Tier  # the higher of the two signals, before the ceiling and before what is configured
    rule_tier: Tier
    label_tier: Tier
    rules: tuple[str, ...]  # ids from RULE_IDS
    label: str | None  # a label LABEL_TIERS knows, else None
    decided_by: DecidedBy  # which signal asked for `asked`: both when they agree above local, none when neither asked for more than local
    limited_by: LimitedBy | None  # why `tier` is lower than `asked`, if it is


def highest_at_or_below(tier: str, configured: Collection[str]) -> Tier:
    """The highest configured tier whose rank is at most `tier`'s. `local` always exists: it is the app's own default model."""
    for candidate in reversed(TIERS[: RANK[tier] + 1]):
        if candidate in configured:
            return candidate
    return "local"


def step_down(tier: str, configured: Collection[str]) -> Tier:
    """The highest configured tier strictly below `tier` (what is used when `tier`'s alias cannot be used), or `local`."""
    return "local" if RANK[tier] == 0 else highest_at_or_below(TIERS[RANK[tier] - 1], configured)


def decide(text: str, label: str | None, *, ceiling: Tier, configured: Collection[str], long_prompt_chars: int) -> Decision:
    """The tier for a message, from its text and the label the service gave it (or `None`). Never raises, and never returns a tier that is not
    `local` or in `configured`."""
    rules = evaluate_rules(text, long_prompt_chars=long_prompt_chars)
    known = label if label in LABEL_TIERS else None
    label_tier: Tier = LABEL_TIERS[known] if known is not None else "local"
    asked: Tier = max(rules.tier, label_tier, key=RANK.__getitem__)
    capped: Tier = min(asked, ceiling, key=RANK.__getitem__)
    tier = highest_at_or_below(capped, configured)
    if RANK[asked] == 0:
        decided_by: DecidedBy = "none"
    elif RANK[rules.tier] == RANK[label_tier]:
        decided_by = "both"
    else:
        decided_by = "rules" if RANK[rules.tier] > RANK[label_tier] else "label"
    # "unconfigured" outranks "ceiling" when both lowered the result: a tier nobody configured is the actionable finding, a ceiling is by design.
    limited_by: LimitedBy | None = "unconfigured" if RANK[tier] < RANK[capped] else ("ceiling" if RANK[capped] < RANK[asked] else None)
    return Decision(tier, asked, rules.tier, label_tier, rules.fired, known, decided_by, limited_by)
