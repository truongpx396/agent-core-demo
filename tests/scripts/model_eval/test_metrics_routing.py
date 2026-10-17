"""The cost-quality curve (RouteLLM's PGR / CPT / APGR) from a four-prompt example worked out by hand.

Prompts 0 and 1 need the strong model (local 0, strong 1); prompts 2 and 3 are fine locally (both 1).
Q_local = 0.5, Q_strong = 1.0, so the gap is 0.5.
"""
import pytest

from scripts.model_eval.metrics import (
    apgr,
    cpt,
    length_router_scores,
    pgr_curve,
    random_router_pgr,
    sampled_random_router_pgr,
)

LOCAL = [0, 0, 1, 1]
STRONG = [1, 1, 1, 1]
GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


def test_an_oracle_router_recovers_the_whole_gap_by_escalating_only_the_prompts_that_need_it():
    # escalate 1 of 4 (prompt 0): quality (1+0+1+1)/4 = 0.75 -> PGR 0.5 ; escalate 2 of 4 (prompts 0, 1): quality 1.0 -> PGR 1
    curve = pgr_curve([0.9, 0.8, 0.2, 0.1], LOCAL, STRONG, GRID)
    assert curve == pytest.approx([0.0, 0.5, 1.0, 1.0, 1.0])
    assert cpt(curve, 0.5, GRID) == 0.25
    assert cpt(curve, 1.0, GRID) == 0.5  # half the calls to the strong model recover all of it
    assert apgr(curve) == pytest.approx(0.7)


def test_a_backwards_router_wastes_escalations_on_prompts_that_did_not_need_them():
    # escalating prompts 3, then 2, gains nothing (both already fine locally); only the 3rd and 4th escalation help
    curve = pgr_curve([0.1, 0.2, 0.8, 0.9], LOCAL, STRONG, GRID)
    assert curve == pytest.approx([0.0, 0.0, 0.0, 0.5, 1.0])
    assert cpt(curve, 1.0, GRID) == 1.0


def test_the_call_performance_threshold_is_none_when_the_gap_is_never_recovered():
    assert cpt([0.0, 0.1, 0.2], 0.5, (0.0, 0.5, 1.0)) is None


def test_the_random_router_recovers_exactly_the_share_it_escalates():
    assert random_router_pgr(GRID) == list(GRID)


def test_the_sampled_random_router_agrees_with_that_expectation_on_a_larger_set():
    n = 40
    q_local = [0.0] * 20 + [1.0] * 20
    q_strong = [1.0] * n
    sampled = sampled_random_router_pgr(q_local, q_strong, trials=300, grid=GRID)
    assert sampled == pytest.approx(list(GRID), abs=0.05)


def test_when_the_strong_model_is_no_better_the_gap_is_undefined_and_that_is_an_error_not_a_zero_division():
    with pytest.raises(ValueError, match="gap is undefined"):
        pgr_curve([0.1, 0.2], [1, 1], [1, 1])


def test_the_length_router_scores_longer_prompts_higher():
    assert length_router_scores(["hi", "a b c d", "a b"]) == [1.0, 4.0, 2.0]
