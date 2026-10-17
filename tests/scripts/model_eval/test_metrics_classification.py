"""Accuracy, macro-F1 and calibration error from hand-worked examples."""
import pytest

from scripts.model_eval.metrics import accuracy, expected_calibration_error, macro_f1


def test_accuracy_is_the_share_of_exact_matches():
    assert accuracy([1, 1, 0, 0], [1, 0, 0, 0]) == 0.75


def test_macro_f1_averages_each_class_equally():
    y_true = ["a", "a", "b", "b", "c", "c"]
    y_pred = ["a", "b", "b", "b", "c", "a"]
    # a: tp 1, fp 1 (the last item), fn 1 -> F1 = 2/(2+1+1) = 0.5
    # b: tp 2, fp 1, fn 0                -> F1 = 4/(4+1+0) = 0.8
    # c: tp 1, fp 0, fn 1                -> F1 = 2/(2+0+1) = 0.6667
    result = macro_f1(y_true, y_pred)
    assert result.value == pytest.approx((0.5 + 0.8 + 2 / 3) / 3)
    assert (result.classes_scored, result.classes_only_predicted) == (3, 0)


def test_a_class_that_is_only_ever_predicted_has_no_support_so_it_is_excluded_and_counted():
    result = macro_f1(["a", "a"], ["a", "z"])
    # a: tp 1, fp 0, fn 1 -> F1 = 2/3 ; z has no true example, so it is not averaged in, but it is reported
    assert result.value == pytest.approx(2 / 3)
    assert (result.classes_scored, result.classes_only_predicted) == (1, 1)


def test_ece_is_the_weighted_gap_between_confidence_and_accuracy_per_bin():
    # 0.9 falls in bin 13 (of 15) where the items are both right: gap 0.1 ; 0.1 in bin 1 where both are wrong: gap 0.1 ; each bin weighs 0.5
    ece, bins = expected_calibration_error([0.9, 0.9, 0.1, 0.1], [True, True, False, False])
    assert ece == pytest.approx(0.1)
    assert bins == 2


def test_a_perfectly_confident_and_correct_model_has_zero_error_and_confidence_one_is_not_dropped():
    ece, bins = expected_calibration_error([1.0, 1.0], [True, True])
    assert ece == 0.0 and bins == 1  # 1.0 lands in the top bin instead of falling off the end


def test_an_overconfident_model_is_penalised():
    ece, _ = expected_calibration_error([0.95, 0.95, 0.95, 0.95], [True, False, False, False])
    # one bin: confidence 0.95, accuracy 0.25 -> gap 0.7, weight 1
    assert ece == pytest.approx(0.7)
