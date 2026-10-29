"""scripts/model_eval/runners/classify.py and the classifier suite, with fake clients and hand-computed numbers (specs/014 T072)."""
import numpy as np
import pytest

from scripts.model_eval import metrics
from scripts.model_eval.candidates import Candidate, Source
from scripts.model_eval.routing import Outcome, write_outcomes
from scripts.model_eval.runners import classify
from scripts.model_eval.runners.base import PrerequisiteMissing
from scripts.model_eval.suites import ClassifierSuite, ItemData

CANDIDATE = Candidate("some-router", "classifier", "service", "MIT", Source(model="classify"))


# ---- the suite ----------------------------------------------------------------------------------------------------------------------
def data(score=(0.9, 0.8, 0.2, 0.1), label=(1, 1, 0, 0), q_local=(0.2, 0.3, 0.7, 0.8), q_strong=(0.9, 0.8, 0.75, 0.85), words=(3, 9, 5, 1)):
    n = len(score)
    return ItemData([f"i{k}" for k in range(n)], {
        "score": np.array(score, dtype=float), "label": np.array(label, dtype=float), "q_local": np.array(q_local, dtype=float),
        "q_strong": np.array(q_strong, dtype=float), "words": np.array(words, dtype=float)})


def test_a_router_that_ranks_exactly_right_has_hand_computed_numbers():
    values = ClassifierSuite().compute(data(), np.arange(4))

    assert values["accuracy"] == 1.0 and values["macro_f1"] == 1.0
    assert values["ece"] == pytest.approx((0.1 + 0.2 + 0.2 + 0.1) / 4)  # confidences 0.9, 0.8, 0.8, 0.9 on four right answers
    # The gap is mean(q_strong) - mean(q_local) = 0.825 - 0.5 = 0.325. Escalating the top 25% (item 0) recovers (0.9 - 0.2) / 4 = 0.175 of it.
    assert metrics.pgr_curve([0.9, 0.8, 0.2, 0.1], [0.2, 0.3, 0.7, 0.8], [0.9, 0.8, 0.75, 0.85])[5] == pytest.approx(0.175 / 0.325)
    assert values["apgr"] > 0.5  # better than a random router
    # CPT is read off the 5%-step grid and a share p escalates round(p * 4) of the four items: 15% is the first share that escalates one item (it
    # recovers 0.175 / 0.325 = 0.54 of the gap, past half), and 40% the first that escalates two (0.3 / 0.325 = 0.92, past 90%).
    assert values["cpt50"] == 0.15 and values["cpt90"] == 0.4


def test_the_length_comparison_is_the_heads_curve_minus_the_longest_first_curve():
    values = ClassifierSuite().compute(data(), np.arange(4))
    ours = metrics.apgr(metrics.pgr_curve([0.9, 0.8, 0.2, 0.1], [0.2, 0.3, 0.7, 0.8], [0.9, 0.8, 0.75, 0.85]))
    longest = metrics.apgr(metrics.pgr_curve([3, 9, 5, 1], [0.2, 0.3, 0.7, 0.8], [0.9, 0.8, 0.75, 0.85]))

    assert values["apgr_vs_length"] == pytest.approx(ours - longest) and values["apgr_vs_length"] > 0  # this router beats escalating the longest first


@pytest.mark.parametrize("seed", range(10))
def test_whenever_the_gap_is_defined_every_router_reaches_half_and_ninety_percent_by_escalating_everything(seed):
    """Escalating every prompt recovers exactly all of the gap, so the cost-quality curve ends at 1.0 and CPT is
    always defined (at worst 100%).
    """
    rng = np.random.default_rng(seed)
    n = 30
    q_local = rng.random(n) * 0.5
    d = data(score=tuple(rng.random(n)), label=tuple(rng.integers(0, 2, n)), q_local=tuple(q_local), q_strong=tuple(q_local + 0.1 + rng.random(n) * 0.3),
             words=tuple(rng.integers(1, 20, n)))

    values = ClassifierSuite().compute(d, np.arange(n))

    assert metrics.pgr_curve(d.arrays["score"], d.arrays["q_local"], d.arrays["q_strong"])[-1] == 1.0
    assert 0.0 < values["cpt50"] <= values["cpt90"] <= 1.0


def test_a_resample_on_which_the_strong_model_is_no_better_has_no_routing_value_but_still_has_a_decision_value():
    values = ClassifierSuite().compute(data(q_strong=(0.2, 0.3, 0.7, 0.8)), np.arange(4))  # strong == local everywhere: no gap to recover

    assert values["accuracy"] == 1.0
    assert all(np.isnan(values[k]) for k in ("apgr", "cpt50", "cpt90", "apgr_vs_length"))


def test_higher_is_better_is_declared_for_every_metric_and_lower_for_the_three_that_should_fall():
    suite = ClassifierSuite()

    assert [n for n in suite.names() if not suite.higher_is_better(n)] == ["ece", "cpt50", "cpt90"]


# ---- the backends -------------------------------------------------------------------------------------------------------------------
def test_the_service_path_is_gone_with_the_trained_head_and_the_message_says_what_to_do_instead():
    with pytest.raises(PrerequisiteMissing, match=r"no longer serves a trained routing head.*run\(backend=\.\.\.\)"):
        classify.build_backend(CANDIDATE, classify.RunSettings(), via="service")
    assert not hasattr(classify, "ServiceClassifier")


# ---- finding the outcomes -----------------------------------------------------------------------------------------------------------
def test_the_only_file_in_the_outcomes_directory_is_the_one_used_and_none_means_unmeasured(tmp_path):
    assert classify.find_outcomes(tmp_path, None) is None
    write_outcomes(tmp_path / "outcomes" / "only.jsonl", [Outcome("a", 0.1, 0.9, "j")])

    assert classify.find_outcomes(tmp_path, None) == tmp_path / "outcomes" / "only.jsonl"


def test_two_files_are_ambiguous_unless_one_is_named(tmp_path):
    for name in ("a.jsonl", "b.jsonl"):
        write_outcomes(tmp_path / "outcomes" / name, [Outcome("a", 0.1, 0.9, "j")])

    with pytest.raises(PrerequisiteMissing, match="pass --outcomes"):
        classify.find_outcomes(tmp_path, None)
    assert classify.find_outcomes(tmp_path, tmp_path / "outcomes" / "b.jsonl") == tmp_path / "outcomes" / "b.jsonl"


def test_a_named_file_that_does_not_exist_is_a_missing_prerequisite(tmp_path):
    with pytest.raises(PrerequisiteMissing, match="does not exist"):
        classify.find_outcomes(tmp_path, tmp_path / "nope.jsonl")
