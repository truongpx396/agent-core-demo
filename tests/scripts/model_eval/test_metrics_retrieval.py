"""Retrieval metrics against values worked out by hand from the definitions (scripts/model_eval/metrics.py)."""
import math

import pytest

from scripts.model_eval.metrics import mean_defined, mrr_at_k, ndcg_at_k, recall_at_k

LOG2_3 = math.log2(3)


def test_ndcg_of_a_perfectly_ordered_list_is_one():
    # gains x=3, y=2 ranked x, y: DCG = 3/log2(2) + 2/log2(3) and the ideal ordering is the same
    assert ndcg_at_k(["x", "y", "z"], {"x": 3, "y": 2}, 3) == pytest.approx(1.0)


def test_ndcg_penalises_putting_the_better_item_second():
    # ranked y, x: DCG = 2/1 + 3/log2(3) = 3.8928; ideal = 3/1 + 2/log2(3) = 4.2619 -> 0.9134
    expected = (2 + 3 / LOG2_3) / (3 + 2 / LOG2_3)
    assert expected == pytest.approx(0.9134, abs=1e-4)
    assert ndcg_at_k(["y", "x", "z"], {"x": 3, "y": 2}, 3) == pytest.approx(expected)


def test_ndcg_ignores_relevant_items_below_the_cutoff_but_the_ideal_still_counts_them():
    # k=2, ranked z, y, x: DCG@2 = 0 + 2/log2(3) = 1.2619; ideal@2 = 3 + 2/log2(3) = 4.2619 -> 0.2961
    assert ndcg_at_k(["z", "y", "x"], {"x": 3, "y": 2}, 2) == pytest.approx((2 / LOG2_3) / (3 + 2 / LOG2_3))


def test_ndcg_with_a_list_shorter_than_k_and_more_relevant_items_than_were_returned():
    # ranked [x] only, relevant x and y (gain 1 each), k=10: DCG = 1; ideal = 1 + 1/log2(3) = 1.6309 -> 0.6131
    assert ndcg_at_k(["x"], {"x": 1, "y": 1}, 10) == pytest.approx(1 / (1 + 1 / LOG2_3))


def test_ndcg_is_none_when_nothing_is_relevant():
    assert ndcg_at_k(["x"], {}, 5) is None
    assert ndcg_at_k(["x"], {"x": 0}, 5) is None  # a zero gain is not relevance


def test_recall_is_the_share_of_relevant_items_found_in_the_top_k():
    ranked, relevant = ["a", "b", "c", "d"], {"a": 1, "d": 1}
    assert recall_at_k(ranked, relevant, 2) == pytest.approx(0.5)
    assert recall_at_k(ranked, relevant, 4) == pytest.approx(1.0)
    assert recall_at_k(ranked, {}, 4) is None


def test_mrr_is_the_reciprocal_rank_of_the_first_relevant_item_inside_the_cutoff():
    ranked, relevant = ["a", "b", "c", "d"], {"c": 1, "d": 1}
    assert mrr_at_k(ranked, relevant, 10) == pytest.approx(1 / 3)
    assert mrr_at_k(ranked, relevant, 2) == 0.0  # the first relevant item is at rank 3, outside k=2
    assert mrr_at_k(ranked, {}, 10) is None


def test_the_mean_is_over_the_queries_that_could_be_scored():
    assert mean_defined([1.0, None, 0.0]) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="unusable"):
        mean_defined([None, None])
