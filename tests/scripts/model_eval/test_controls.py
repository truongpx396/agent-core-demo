"""Controls: they must catch a broken comparison. A harness whose controls pass for the wrong reason is worse than none."""
import numpy as np

from scripts.model_eval.controls import (
    CONTROL_CANDIDATES,
    derived_controls,
    judge,
    judge_candidate_control,
)
from scripts.model_eval.stats import Delta, compare
from scripts.model_eval.suites import EMBEDDING_SUITE, ItemData, ScreenSuite


def _embedding_baseline(n=120, seed=3) -> ItemData:
    rng = np.random.default_rng(seed)
    arrays = {m: rng.random(n) * 0.5 + 0.4 for m in EMBEDDING_SUITE.names()}
    return ItemData([f"q{i}" for i in range(n)], arrays)


def _screen_baseline(n=240, seed=5) -> ItemData:
    rng = np.random.default_rng(seed)
    label = (np.arange(n) % 2).astype(float)  # half attacks
    score = np.where(label == 1, rng.beta(8, 2, n), rng.beta(2, 8, n))  # a decent screen: attacks score high
    return ItemData(
        [f"s{i}" for i in range(n)],
        {"score": score, "label": label, "domain": (rng.random(n) < 0.7).astype(float), "pattern_hit": (rng.random(n) < 0.1).astype(float)},
    )


def test_every_report_derives_an_identity_control_and_a_degenerate_one_for_a_mean_suite():
    results = derived_controls("embedding", EMBEDDING_SUITE, _embedding_baseline())
    assert [(r.name, r.expect, r.ok) for r in results] == [("identity", "no_difference", True), ("retrieves-nothing", "worse", True)]


def test_the_screen_controls_are_judged_on_the_detection_metrics_only():
    suite = ScreenSuite(0.5)
    results = derived_controls("screen", suite, _screen_baseline())
    assert {r.name for r in results} == {"identity", "random-scores", "always-benign"}
    assert all(r.ok for r in results), [r.as_dict() for r in results]
    # an always-benign screen has a PERFECT false-positive rate, so judging it on FPR would call a working harness broken
    benign = next(r for r in results if r.name == "always-benign")
    assert benign.observed == "worse"


def test_a_comparison_that_cannot_tell_a_model_from_itself_fails_the_identity_control():
    suite = EMBEDDING_SUITE
    base = _embedding_baseline()
    shifted = ItemData(list(base.ids), {k: v + 0.2 for k, v in base.arrays.items()})
    result = judge("identity", "no_difference", compare(suite, shifted, base))
    assert not result.ok and result.observed == "better"  # a harness reporting "better" for an unchanged model is broken


def test_a_degenerate_model_reported_as_equal_fails_the_worse_control():
    suite = EMBEDDING_SUITE
    base = _embedding_baseline()
    result = judge("retrieves-nothing", "worse", compare(suite, base, base))  # "retrieves nothing" accidentally equal to the baseline
    assert not result.ok


def test_inconclusive_counts_as_no_difference_for_a_real_model_run_twice():
    deltas = {"x": Delta(0.001, (-0.02, 0.02), "inconclusive", 900)}
    assert judge("baseline-as-candidate", "no_difference", deltas).ok  # float noise on a non-deterministic backend is not a failure
    assert not judge("baseline-as-candidate", "no_difference", {"x": Delta(0.1, (0.05, 0.15), "better", None)}).ok


def test_a_control_candidate_is_recognised_by_name_and_other_names_are_not_controls():
    deltas = {m: Delta(-0.3, (-0.4, -0.2), "worse", None) for m in EMBEDDING_SUITE.names()}
    assert judge_candidate_control("random-vectors", "embedding", deltas).ok
    assert judge_candidate_control("bge-small-en-v1.5", "embedding", deltas) is None
    assert {"baseline-as-candidate"} <= set(CONTROL_CANDIDATES["screen"]) & set(CONTROL_CANDIDATES["embedding"])
