"""The constitution's LLM-access rule and the guidance that restates it must agree (spec 014 FR-040, Governance).

Spec 014 added one optional, operator-configured escalation alias (`STRONG_CHAT_MODEL`) and spec 015 a second routed tier (`MEDIUM_CHAT_MODEL`). The old wording ("a cloud model MAY be used
only for eval and red-team grading roles, never as the target model or a runtime dependency") forbade even that, so the constitution
was amended to 1.2.0 (and to 1.3.0 for the second alias). The Governance section says the guidance files MUST NOT contradict it; these tests fail on the two ways that
can drift: the amendment losing the clause that makes the seam lawful (or the offline guarantee it must not weaken), and a guidance
file still carrying the superseded sentence.

Text-only: no Docker, no network.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONSTITUTION = REPO / ".specify" / "memory" / "constitution.md"
# The files that describe how contributors and agents work here. Governance: they MUST NOT contradict the constitution.
GUIDANCE = [
    "CLAUDE.md",
    "README.md",
    "GRAPH_PATTERNS.md",
    "COST_GOVERNANCE.md",
    "ENGINEERING_PATTERNS.md",
    *sorted(str(p.relative_to(REPO)) for p in (REPO / ".claude" / "rules").glob("*.md")),
    *sorted(str(p.relative_to(REPO)) for p in (REPO / "deploy" / "litellm").glob("*.yaml")),
]
SUPERSEDED = re.compile(r"never as the target model|only for eval and\s+red-team grading", re.IGNORECASE)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_constitution_permits_exactly_two_optional_routed_aliases():
    text = _flat(CONSTITUTION.read_text())
    assert "`MEDIUM_CHAT_MODEL`" in text and "`STRONG_CHAT_MODEL`" in text
    assert "one of TWO optional, operator-configured routed aliases" in text
    # What keeps the seam lawful: off by default, never a dependency, a failure degrades to the local model.
    assert "Each is off unless an operator sets it" in text
    assert "a failure of it MUST degrade to the local model" in text
    assert "ONE optional" not in text, "the 1.2.0 wording (a single escalation alias) is superseded"


def test_the_amendment_does_not_weaken_the_offline_guarantee_or_the_advisory_limit():
    text = _flat(CONSTITUTION.read_text())
    assert "The core MUST run fully offline against local Ollama" in text
    assert "The routing decision is advisory only" in text
    # It names the principles it must not touch, so a reader of the bullet finds them.
    assert "(Principles I, II, IV and VI)" in text


def test_the_superseded_sentence_is_gone_from_the_constitution():
    assert not SUPERSEDED.search(_flat(CONSTITUTION.read_text()))


def test_no_guidance_file_still_carries_the_superseded_rule():
    offenders = [name for name in GUIDANCE if SUPERSEDED.search(_flat((REPO / name).read_text()))]
    assert offenders == [], f"these files still state the pre-1.2.0 LLM-access rule: {offenders}"


def test_the_amendments_scratch_report_is_not_left_in_the_file():
    """The skill writes a Sync Impact Report as an HTML comment for review; it belongs in the PR description, not the constitution."""
    assert "Sync Impact Report" not in CONSTITUTION.read_text()
