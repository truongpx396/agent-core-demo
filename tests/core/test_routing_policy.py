"""app/core/routing_policy.py: the rules, the label table and `decide` (specs/015 FR-022, FR-023; contracts/tiered-router.md invariant 3).

Pure functions, so every case is a value in and a value out. `decide` is checked two ways: hand-worked cases that say what the answer is and
why, and a sweep over EVERY combination of rule tier, label, ceiling and configured tiers against an oracle written out in the test (rank
arithmetic, no shared code), which also proves the one property the graph relies on: it never returns a tier that does not exist.
"""
import itertools
import json
from pathlib import Path

import pytest

from app.core import routing_policy as rp
from app.core.routing_policy import (
    LABEL_TIERS,
    RANK,
    RULE_IDS,
    TIERS,
    Decision,
    decide,
    evaluate_rules,
)

REPO = Path(__file__).resolve().parents[2]
LONG = 1500


def rules(text: str, long_prompt_chars: int = LONG) -> rp.Rules:
    return evaluate_rules(text, long_prompt_chars=long_prompt_chars)


# ---- the label table --------------------------------------------------------------------------------------------------------------
def test_the_label_table_names_exactly_the_labels_of_the_shipped_set():
    shipped = json.loads((REPO / "docker" / "ml-service" / "labelsets" / "route-topics.json").read_text())["labels"]

    assert set(LABEL_TIERS) == set(shipped), "a label with no tier is silently local, and a tier for a label that does not exist is dead policy"
    assert set(LABEL_TIERS.values()) <= set(TIERS)


def test_every_label_has_a_description_for_a_reader_that_has_only_words():
    """The question sent to /v1/decide offers these as its options, and the service refuses a question whose options are not exactly the set's labels,
    so a label with no words (or words for a label that is gone) would turn every routed turn's label call into a 400."""
    assert set(rp.LABEL_DESCRIPTIONS) == set(LABEL_TIERS)
    assert all(isinstance(words, str) and 10 <= len(words) <= 120 for words in rp.LABEL_DESCRIPTIONS.values())
    assert rp.ROUTE_INSTRUCTIONS.strip()


def test_the_label_table_sends_distinctive_work_up_and_everyday_work_nowhere():
    assert {label: tier for label, tier in LABEL_TIERS.items() if tier == "local"} == {
        "smalltalk": "local", "lookup": "local", "action": "local", "drafting": "local"}
    assert {label for label, tier in LABEL_TIERS.items() if tier == "medium"} == {"analysis", "code"}
    assert {label for label, tier in LABEL_TIERS.items() if tier == "strong"} == {"reasoning", "high_stakes"}


# ---- the rules ----------------------------------------------------------------------------------------------------------------------
def test_an_ordinary_question_fires_no_rule():
    assert rules("What are your support hours on weekends?") == rp.Rules("local", ())
    assert rules("") == rp.Rules("local", ())


def test_the_long_prompt_rule_fires_at_the_threshold_and_not_one_character_before():
    assert rules("x" * (LONG - 1)).fired == () and rules("x" * LONG) == rp.Rules("medium", ("long_prompt",))
    assert rules("x" * 300, long_prompt_chars=300).fired == ("long_prompt",) and rules("x" * 299, long_prompt_chars=300).fired == ()


@pytest.mark.parametrize(
    "text",
    [
        "Why does this fail?\n```python\nprint(x)\n```",
        "Traceback (most recent call last):\n  File \"a.py\", line 1\nValueError: nope",
        "What does this do?\n    a = 1\n    b = 2\n    c = 3",
        "What does this do?\n\ta = 1\n\tb = 2\n\tc = 3",
    ],
)
def test_the_code_rule_fires_on_a_fence_a_traceback_or_three_indented_lines(text):
    assert rules(text) == rp.Rules("medium", ("code",))


@pytest.mark.parametrize(
    "text",
    [
        "What does this do?\n    a = 1\n    b = 2",  # two indented lines are a quoted address or a list, not code
        "The Traceback (most recent call last) text mid-sentence is not a traceback header",
        "Use `inline code` here",  # one backtick is not a fence
        "Use ``two`` backticks",
    ],
)
def test_the_code_rule_does_not_fire_on_near_misses(text):
    assert "code" not in rules(text).fired


def test_the_multi_part_rule_fires_on_three_questions_or_three_enumerated_lines():
    assert rules("What is it? Who owns it? When is it due?") == rp.Rules("medium", ("multi_part",))
    assert rules("Please check:\n1. the invoice\n2. the refund\n3. the address") == rp.Rules("medium", ("multi_part",))
    assert rules("Please check:\n- the invoice\n- the refund\n- the address").fired == ("multi_part",)
    assert rules("Please check:\n* the invoice\n• the refund\n1) the address").fired == ("multi_part",)


def test_the_multi_part_rule_does_not_fire_on_two_questions_or_two_items():
    assert rules("What is it? Who owns it?").fired == ()
    assert rules("Please check:\n1. the invoice\n2. the refund").fired == ()


@pytest.mark.parametrize(("text", "tier"), [
    ("Please explain this step by step.", "local"),  # one marker is an ordinary request
    ("Explain step by step and justify it.", "medium"),  # two distinct markers
    ("Walk me through it step by step, justify each choice and list the trade-offs.", "strong"),  # three
    ("Compare and contrast the plans, weigh the trade-offs and justify your pick.", "strong"),
])
def test_the_reasoning_markers_rule_counts_distinct_markers(text, tier):
    got = rules(text)

    assert got.tier == tier and (got.fired == ("reasoning_markers",)) == (tier != "local")


def test_the_same_marker_repeated_is_one_marker():
    assert rules("justify this, justify that, and justify the other").tier == "local"


def test_markers_are_word_bounded_and_case_insensitive():
    assert rules("How can I improve my approach to the preview?").fired == ()  # "improve" and "preview" do not contain "prove" as a word
    assert rules("How can I improve this? Justify the answer.").fired == ()  # ... so only "justify" counts, and one marker is an ordinary request
    assert rules("Prove this. Justify the answer.").fired == ("reasoning_markers",)  # while a real "prove" plus "justify" is two
    assert rules("PROVE it STEP BY STEP").tier == "medium"
    assert rules("Optimize it and optimise it and think this through").tier == "medium"  # the two spellings are one marker; "think this through" is the second


def test_several_rules_fire_together_and_the_highest_tier_wins():
    text = "Why does this fail? Who wrote it? When?\n```\ncode\n```\nExplain step by step, justify it and weigh the trade-offs."

    got = rules(text)

    assert got.fired == ("code", "multi_part", "reasoning_markers") and got.tier == "strong"
    assert list(got.fired) == [r for r in RULE_IDS if r in got.fired], "fired rules are reported in the closed list's order"


def test_a_huge_paste_is_scanned_only_to_a_bound_but_its_length_still_counts():
    padding = "x" * rp.MAX_SCAN_CHARS
    buried = padding + "\n```\ncode\n```\nstep by step, justify, trade-offs"

    assert rules(buried) == rp.Rules("medium", ("long_prompt",)), "the code and the markers are past the scan bound, the length is not"
    assert rules("```\ncode\n```\n" + padding).fired == ("long_prompt", "code")


@pytest.mark.parametrize("text", ["\x00\x01", "日本語の質問です？？？", "🙂" * 5000, "\n" * 10_000, "a?" * 40_000])
def test_rules_never_raise_on_odd_text(text):
    assert isinstance(rules(text), rp.Rules)


# ---- decide: hand-worked cases ---------------------------------------------------------------------------------------------------
ALL = TIERS


def d(text="hello", label=None, ceiling="strong", configured=ALL) -> Decision:
    return decide(text, label, ceiling=ceiling, configured=configured, long_prompt_chars=LONG)


def test_nothing_asking_for_more_stays_local_and_nobody_decided():
    got = d()

    assert (got.tier, got.asked, got.decided_by, got.limited_by, got.rules, got.label) == ("local", "local", "none", None, (), None)


def test_a_label_alone_can_ask_for_a_tier():
    got = d(label="analysis")

    assert (got.tier, got.decided_by, got.label_tier, got.rule_tier) == ("medium", "label", "medium", "local")


def test_the_rules_alone_can_ask_for_a_tier():
    got = d(text="x" * LONG)

    assert (got.tier, got.decided_by, got.rules) == ("medium", "rules", ("long_prompt",))


def test_the_higher_of_the_two_wins_and_a_missing_signal_does_not_cancel_the_other():
    assert d(text="x" * LONG, label="reasoning").tier == "strong"  # the label asks for more than the rules
    assert d(text="step by step, justify, trade-offs", label="lookup").tier == "strong"  # the rules ask for more than the label
    assert d(text="step by step, justify, trade-offs", label=None).tier == "strong"  # no label: the rules alone


def test_two_signals_that_agree_above_local_are_both_the_reason():
    assert d(text="x" * LONG, label="code").decided_by == "both"
    assert d(text="x" * LONG, label="smalltalk").decided_by == "rules"  # agreeing at local is not "both": only the rules asked


def test_the_ceiling_lowers_the_result_and_says_so():
    got = d(label="reasoning", ceiling="medium")

    assert (got.tier, got.asked, got.limited_by) == ("medium", "strong", "ceiling")
    assert d(label="reasoning", ceiling="local").tier == "local"


def test_a_tier_nobody_configured_steps_down_to_the_highest_one_that_exists():
    only_strong = d(label="analysis", configured=("local", "strong"))
    only_medium = d(label="reasoning", configured=("local", "medium"))
    nothing = d(label="reasoning", configured=("local",))

    assert (only_strong.tier, only_strong.limited_by) == ("local", "unconfigured")  # medium was asked for and does not exist: cheaper, not dearer
    assert (only_medium.tier, only_medium.asked, only_medium.limited_by) == ("medium", "strong", "unconfigured")
    assert (nothing.tier, nothing.limited_by) == ("local", "unconfigured")


def test_an_unconfigured_tier_is_reported_ahead_of_a_ceiling_that_also_applied():
    got = d(label="reasoning", ceiling="medium", configured=("local", "strong"))

    assert got.tier == "local" and got.limited_by == "unconfigured"


def test_an_unknown_label_is_no_label_and_is_not_echoed():
    got = d(label="a-label-from-a-set-this-app-was-not-told-about")

    assert got.label is None and got.label_tier == "local" and got.tier == "local"


def test_local_is_always_available_even_if_a_caller_forgets_to_list_it():
    assert d(label="reasoning", configured=()).tier == "local"


def test_the_input_is_not_changed():
    configured = ["local", "medium"]
    d(configured=configured)

    assert configured == ["local", "medium"]


# ---- decide: every combination against an oracle ----------------------------------------------------------------------------------
TEXTS = {
    "local": "hello there",
    "medium": "x" * LONG,
    "strong": "step by step, justify it and weigh the trade-offs",
}
LABELS = [None, "smalltalk", "lookup", "action", "drafting", "analysis", "code", "reasoning", "high_stakes", "unknown-label"]
CONFIGURED = [c for n in range(3) for c in itertools.combinations(("medium", "strong"), n)]


def oracle(rule_tier, label, ceiling, configured):
    label_rank = RANK[LABEL_TIERS[label]] if label in LABEL_TIERS else 0
    target = min(max(RANK[rule_tier], label_rank), RANK[ceiling])
    return max(r for r in range(target + 1) if r == 0 or TIERS[r] in configured)


@pytest.mark.parametrize("rule_tier", TIERS)
@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("ceiling", TIERS)
@pytest.mark.parametrize("configured", CONFIGURED, ids=lambda c: "+".join(("local", *c)))
def test_decide_equals_the_rank_arithmetic_and_never_returns_a_tier_that_does_not_exist(rule_tier, label, ceiling, configured):
    got = d(text=TEXTS[rule_tier], label=label, ceiling=ceiling, configured=("local", *configured))

    assert got.rule_tier == rule_tier, "the sweep's text must ask for the tier the case says it does"
    assert RANK[got.tier] == oracle(rule_tier, label, ceiling, configured)
    assert got.tier == "local" or got.tier in configured
    assert RANK[got.tier] <= RANK[ceiling] and RANK[got.tier] <= RANK[got.asked]
    assert (got.limited_by is None) == (got.tier == got.asked)


# ---- the helpers ------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tier", "configured", "expected"),
    [("strong", ("local", "medium", "strong"), "medium"), ("strong", ("local", "strong"), "local"), ("medium", ("local", "medium"), "local"),
     ("local", ("local", "medium", "strong"), "local"), ("strong", (), "local")],
)
def test_step_down_is_the_highest_configured_tier_strictly_below(tier, configured, expected):
    assert rp.step_down(tier, configured) == expected


@pytest.mark.parametrize(
    ("tier", "configured", "expected"),
    [("strong", ("medium", "strong"), "strong"), ("strong", ("medium",), "medium"), ("medium", ("strong",), "local"), ("local", ("medium", "strong"), "local")],
)
def test_highest_at_or_below_is_the_highest_configured_tier_not_above(tier, configured, expected):
    assert rp.highest_at_or_below(tier, configured) == expected
