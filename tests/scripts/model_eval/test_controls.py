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


def _marginal_screen_baseline(n=67, seed=11) -> ItemData:
    """Shaped like the committed prompt-guard baseline (67 items, 32 attacks): decent ranking but, at the 0.5 threshold, it flags only about
    half of the attacks. That is the case the first control got wrong."""
    rng = np.random.default_rng(seed)
    label = (np.arange(n) < 32).astype(float)
    score = np.where(label == 1, rng.beta(3, 3.2, n), rng.beta(1.2, 6, n))
    return ItemData([f"s{i}" for i in range(n)],
                    {"score": score, "label": label, "domain": (label == 0).astype(float), "pattern_hit": np.zeros(n)})


def test_a_random_screen_is_not_judged_on_recall_because_at_the_threshold_it_recalls_about_as_much_as_a_real_one():
    """Regression: `make model-eval-verify` failed on the committed prompt-guard baseline ("random-scores
    expected worse, observed inconclusive").
    """
    suite = ScreenSuite(0.5)
    baseline = _marginal_screen_baseline()
    point = suite.compute(baseline, np.arange(len(baseline)))
    assert 0.3 < point["ml_recall@thr"] < 0.7, "test setup: the baseline must be a marginal-recall screen"

    results = {r.name: r for r in derived_controls("screen", suite, baseline)}

    assert results["random-scores"].ok, results["random-scores"].as_dict()
    broken = ItemData(list(baseline.ids), {**baseline.arrays, "score": np.random.default_rng(20261017).random(len(baseline))})
    recall = compare(suite, broken, baseline)["ml_recall@thr"]
    assert recall.verdict != "worse"  # the premise: judged on recall this control would have failed


def test_each_degenerate_screen_is_judged_on_what_it_cannot_do():
    from scripts.model_eval.controls import _JUDGED_ON

    assert set(_JUDGED_ON[("screen", "random-scores")]) == {"roc_auc", "ml_fpr_domain@thr", "app_fpr_domain@thr"}  # ranking and false alarms
    assert set(_JUDGED_ON[("screen", "always-benign")]) == {"roc_auc", "tpr@1%fpr", "ml_recall@thr", "app_recall@thr"}  # it detects nothing
    assert "tpr@1%fpr" not in _JUDGED_ON[("screen", "random-scores")]  # a few dozen benign items make it seed-dependent for a random scorer


def test_the_random_scores_candidate_control_uses_the_same_metrics_as_the_derived_one():
    suite = ScreenSuite(0.5)
    deltas = {m: Delta(-0.3, (-0.4, -0.2), "worse", None) for m in ("roc_auc", "ml_fpr_domain@thr", "app_fpr_domain@thr")}
    deltas["ml_recall@thr"] = Delta(0.03, (-0.2, 0.25), "inconclusive", 500)  # a random scorer recalls about as much as a real one
    assert judge_candidate_control("random-scores", "screen", deltas).ok
    assert set(suite.names()) >= set(deltas)


def test_a_degenerate_model_whose_difference_cannot_be_told_from_noise_fails_its_worse_control():
    """"Worse" must mean worse. A useless model that comes out `inconclusive` (the dataset is too small to separate it) would otherwise pass
    for a harness that works; the control has to fail so the report is marked invalid and nobody trusts it."""
    result = judge("random-vectors", "worse", {"ndcg@10": Delta(-0.02, (-0.2, 0.15), "inconclusive", 800)})
    assert not result.ok and result.observed == "inconclusive"
