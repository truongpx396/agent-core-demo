"""Bootstrap intervals and verdicts (scripts/model_eval/stats.py)."""
import numpy as np
import pytest

from scripts.model_eval.stats import SEED, compare, measure, resample_indices
from scripts.model_eval.suites import EMBEDDING_SUITE, ItemData, MeanSuite, ScreenSuite

SUITE = MeanSuite("embedding", ("score",))


def _data(values, ids=None) -> ItemData:
    values = np.asarray(values, dtype=float)
    return ItemData(ids or [f"q{i}" for i in range(len(values))], {"score": values})


def test_the_same_seed_gives_the_same_resamples_and_the_same_interval():
    assert np.array_equal(resample_indices(50), resample_indices(50))
    a, b = measure(SUITE, _data(np.linspace(0, 1, 50))), measure(SUITE, _data(np.linspace(0, 1, 50)))
    assert a["score"].ci95 == b["score"].ci95 and SEED == 20261017


def test_the_interval_brackets_the_mean_and_narrows_with_more_items():
    rng = np.random.default_rng(1)
    small = measure(SUITE, _data(rng.random(30)))["score"]
    large = measure(SUITE, _data(rng.random(600)))["score"]
    assert small.ci95[0] <= small.value <= small.ci95[1]
    assert (large.ci95[1] - large.ci95[0]) < (small.ci95[1] - small.ci95[0])


def test_identical_per_item_values_are_no_difference_not_inconclusive():
    delta = compare(SUITE, _data([0.2, 0.4, 0.9, 0.5]), _data([0.2, 0.4, 0.9, 0.5]))["score"]
    assert delta.verdict == "no_difference" and delta.delta == 0.0 and delta.needed_n is None


def test_a_consistent_improvement_across_items_is_better_even_when_the_means_are_close():
    base = np.linspace(0.2, 0.8, 200)
    delta = compare(SUITE, _data(base + 0.01), _data(base))["score"]  # +0.01 on EVERY item: tiny but certain
    assert delta.verdict == "better" and delta.ci95[0] > 0


def test_a_clear_regression_is_worse():
    base = np.linspace(0.2, 0.8, 200)
    assert compare(SUITE, _data(base - 0.1), _data(base))["score"].verdict == "worse"


def test_a_noisy_small_difference_is_inconclusive_and_says_how_many_more_items_it_would_take():
    rng = np.random.default_rng(7)
    base = rng.random(40)
    cand = base + rng.normal(0.01, 0.3, 40)  # mean +0.01 buried in per-item noise
    delta = compare(SUITE, _data(cand), _data(base))["score"]
    assert delta.verdict == "inconclusive"
    assert delta.needed_n is not None and delta.needed_n > 40


def test_a_lower_is_better_metric_flips_the_direction_of_the_verdict():
    suite = ScreenSuite(threshold=0.5)
    n = 200
    common = {"label": np.zeros(n), "domain": np.ones(n), "pattern_hit": np.zeros(n)}
    ids = [f"i{k}" for k in range(n)]
    low = ItemData(ids, {"score": np.full(n, 0.1), **common})
    high = ItemData(ids, {"score": np.full(n, 0.9), **common})
    # all benign: a screen that flags everything has a HIGHER false-positive rate, which is WORSE for a lower-is-better metric
    assert compare(suite, high, low)["ml_fpr_domain@thr"].verdict == "worse"


def test_two_runs_that_share_too_few_items_are_not_the_same_evaluation():
    a, b = _data([0.1] * 20, [f"a{i}" for i in range(20)]), _data([0.1] * 20, [f"b{i}" for i in range(20)])
    with pytest.raises(ValueError, match="not the same evaluation"):
        compare(SUITE, a, b)


def test_items_are_aligned_by_id_so_order_does_not_matter():
    base = _data([0.1, 0.5, 0.9], ["x", "y", "z"])
    shuffled = _data([0.9, 0.1, 0.5], ["z", "x", "y"])
    assert compare(SUITE, shuffled, base)["score"].verdict == "no_difference"


def test_duplicate_item_ids_are_refused_because_alignment_is_by_id():
    with pytest.raises(ValueError, match="unique"):
        _data([0.1, 0.2], ["a", "a"])


def test_the_embedding_suite_names_the_metrics_the_app_cares_about():
    assert EMBEDDING_SUITE.names() == ["recall@5", "recall@20", "mrr@10", "ndcg@10"]


def test_the_interval_is_the_two_and_a_half_to_ninety_seven_and_a_half_percentile():
    from scripts.model_eval.stats import _interval

    assert _interval(np.arange(1001.0)) == (25.0, 975.0)  # 95% central: a 90% or 99% interval would print different numbers


@pytest.mark.parametrize(("lo", "hi"), [(0.0, 0.1), (-0.1, 0.0)])
def test_an_interval_that_only_touches_zero_is_not_significant(lo, hi):
    from scripts.model_eval.stats import _verdict

    verdict, needed = _verdict(0.05, lo, hi, True, False, 100)

    assert verdict == "inconclusive" and needed is not None  # touching zero is "cannot rule out no change", and it says how many more items would help
