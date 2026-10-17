"""Screening metrics: ROC-AUC and detection at a fixed false-positive rate, from hand-worked examples."""
import pytest

from scripts.model_eval.metrics import (
    precision_at_threshold,
    rate_at_threshold,
    roc_auc,
    tpr_at_fpr,
)


def test_a_perfectly_separating_screen_has_auc_one_and_detects_everything_at_zero_false_positives():
    scores, labels = [0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]
    assert roc_auc(scores, labels) == 1.0
    assert tpr_at_fpr(scores, labels, 0.01) == 1.0


def test_a_screen_that_scores_everything_the_same_has_auc_one_half():
    assert roc_auc([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == 0.5


def test_auc_counts_a_tie_between_a_positive_and_a_negative_as_half():
    # positives {0.5, 0.9}, negatives {0.5, 0.1}: pairs (0.5,0.5)=0.5, (0.5,0.1)=1, (0.9,0.5)=1, (0.9,0.1)=1 -> 3.5/4
    assert roc_auc([0.5, 0.5, 0.1, 0.9], [1, 0, 0, 1]) == pytest.approx(0.875)


def test_tpr_at_a_false_positive_cap_takes_the_best_threshold_that_stays_under_the_cap():
    scores = [0.9, 0.5, 0.3, 0.8, 0.4, 0.2]  # positives 0.9, 0.5, 0.3 ; negatives 0.8, 0.4, 0.2
    labels = [1, 1, 1, 0, 0, 0]
    # A 1% cap with 3 negatives allows no false positive at all: only the threshold above 0.8 qualifies -> catches 0.9 only.
    assert tpr_at_fpr(scores, labels, 0.01) == pytest.approx(1 / 3)
    # A 50% cap allows one of three negatives (33%): threshold 0.5 flags negatives {0.8} and positives {0.9, 0.5} -> 2/3.
    assert tpr_at_fpr(scores, labels, 0.5) == pytest.approx(2 / 3)


def test_a_screen_that_cannot_stay_under_the_cap_scores_zero_because_flagging_nothing_is_always_allowed():
    assert tpr_at_fpr([0.1, 0.9], [1, 0], 0.01) == 0.0


def test_the_rate_at_a_threshold_is_the_share_at_or_above_it():
    assert rate_at_threshold([0.1, 0.5, 0.9], 0.5) == pytest.approx(2 / 3)  # the boundary value counts as flagged, as the app's >= does


def test_precision_at_a_threshold_is_the_positive_share_of_what_was_flagged():
    assert precision_at_threshold([0.9, 0.6, 0.2], [1, 0, 0], 0.5) == pytest.approx(0.5)
    assert precision_at_threshold([0.1, 0.2], [1, 0], 0.5) is None  # nothing flagged: precision is undefined, not zero


def test_a_set_with_only_one_class_is_refused_because_no_screen_can_be_scored_on_it():
    with pytest.raises(ValueError, match="at least one positive and one negative"):
        roc_auc([0.1, 0.2], [0, 0])
