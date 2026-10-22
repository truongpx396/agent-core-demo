"""scripts/model_eval/runners/topics.py: scoring a label set on sentences with a known topic (specs/015 FR-010..012).

A fake matcher stands in for the embedding model, so what is under test is the runner: how it builds the per-item arrays the suite reads, which
cutoff it uses, what it refuses, and how the calibration is chosen. The numbers are worked out by hand from the committed `route-topics` dataset
(8 topics and `none`; 10 test sentences per topic and 16 `none`).
"""
import json

import numpy as np
import pytest

pytest.importorskip("prometheus_client", reason="the runner parses label sets with the ML service's own parser, which imports prometheus-client")

from scripts.model_eval import (
    __main__ as cli,  # noqa: E402 - after the importorskip above, on purpose
)
from scripts.model_eval import datasets as ds  # noqa: E402
from scripts.model_eval.controls import derived_controls  # noqa: E402
from scripts.model_eval.runners import classify, topics  # noqa: E402
from scripts.model_eval.runners.base import (  # noqa: E402
    PrerequisiteMissing,
    RunSettings,
)
from scripts.model_eval.stats import measure  # noqa: E402
from scripts.model_eval.suites import IntentSuite, suite_for  # noqa: E402

CANDIDATE = cli.resolve_candidate("classifier", "labelset-bge-small-en-v1.5")
LABELSET = topics.load_labelset("route-topics")
LABELS = tuple(LABELSET.labels)
MANIFEST = ds.load_verified(ds.DATASETS_ROOT / "route-topics" / "v1")
CUTOFF = 0.6


class FakeTopics:
    """A matcher that knows the answer: the true topic at 0.9 for an in-scope sentence, nothing close (0.1) for a `none` one. `mode` breaks it."""

    identity, revision, size_bytes, rss_mb = "fake:model", "r" * 40, 123, 10.0
    labels = LABELS
    labelset_version = "fakeversion1"

    def __init__(self, mode: str = "perfect", min_similarity: float | None = CUTOFF) -> None:
        self.mode, self.min_similarity = mode, min_similarity
        truth = {r["text"]: r["label"] for split in ("dev", "test") for r in MANIFEST.split_items(split)}
        self.truth = truth

    def nearest(self, texts):
        out = []
        for text in texts:
            label = self.truth[text]
            if self.mode == "constant":
                out.append((0, 0.9))  # always the first topic, always confident
            elif label == "none":
                out.append((0, 0.1))
            else:
                out.append((LABELS.index(label), 0.9))
        return out


def settings(**kw) -> RunSettings:
    return RunSettings(dataset="route-topics", runs=1, **kw)


# ---- the numbers --------------------------------------------------------------------------------------------------------------------
def test_a_matcher_that_knows_every_topic_and_rejects_every_none_scores_one_everywhere():
    output = topics.run(CANDIDATE, settings(), backend=FakeTopics())
    values = {name: mv.value for name, mv in measure(IntentSuite(), output.data).items()}

    assert len(output.data) == 96
    assert values["accuracy_in_scope"] == 1.0 and values["macro_f1_in_scope"] == 1.0
    assert values["oos_recall@thr"] == 1.0 and values["in_scope_reject@thr"] == 0.0


def test_the_run_is_reported_as_a_classifier_run_scored_by_the_intent_suite_with_the_labels_cutoff():
    output = topics.run(CANDIDATE, settings(), backend=FakeTopics())

    assert output.kind == "classifier" and isinstance(suite_for("classifier", dataset=output.dataset["name"]), IntentSuite)
    assert set(output.data.arrays) == {"pred", "label", "confidence", "oos", "oos_threshold"}
    assert set(output.data.arrays["oos_threshold"]) == {CUTOFF}
    assert output.data.arrays["oos"].sum() == 16 and (output.data.arrays["label"] == -1).sum() == 16
    assert output.dataset["name"] == "route-topics" and output.dataset["reviewed_share"] == 0.0  # authored and unreviewed: the report says PROVISIONAL
    assert output.candidate["identity"] == "fake:model" and output.candidate["revision"] == "r" * 40
    assert output.efficiency["query_ms"]["p95"] >= 0 and output.efficiency["rss_mb"] == 10.0


def test_a_perfect_matcher_passes_the_shuffled_predictions_control_and_a_constant_one_fails_it():
    suite = IntentSuite()
    good = topics.run(CANDIDATE, settings(), backend=FakeTopics())
    bad = topics.run(CANDIDATE, settings(), backend=FakeTopics("constant"))

    assert all(c.ok for c in derived_controls("classifier", suite, good.data))
    failed = [c.name for c in derived_controls("classifier", suite, bad.data) if not c.ok]
    assert failed == ["shuffled-predictions"], "predicting one topic for everything is no better than the same answers handed to the wrong sentences"


def test_the_trade_offs_name_the_label_set_the_cutoff_per_topic_results_and_the_worst_mix_ups():
    lines = topics.run(CANDIDATE, settings(), backend=FakeTopics("constant")).tradeoffs

    assert lines[0].startswith("label set route-topics fakeversion1: 8 labels, 80 examples, cutoff 0.6")
    assert any("smalltalk 10/10" in line and "lookup 0/10" in line for line in lines)  # the constant matcher always says the first topic
    assert any("-> smalltalk x10" in line for line in lines if line.startswith("most common mix-ups"))
    assert any("ece_in_scope" in line and "not meaningful" in line for line in lines)


# ---- what it refuses ----------------------------------------------------------------------------------------------------------------
def test_a_label_set_with_no_cutoff_for_the_candidates_model_cannot_be_scored():
    with pytest.raises(PrerequisiteMissing, match="no calibration for 'bge-small-en-v1.5'.*labelset-calibrate"):
        topics.run(CANDIDATE, settings(), backend=FakeTopics(min_similarity=None))


@pytest.mark.parametrize("name", ["constant", "random-router", "baseline-as-candidate"])
def test_the_routing_controls_are_not_label_set_candidates(name):
    control = cli.resolve_candidate("classifier", name)
    with pytest.raises(PrerequisiteMissing, match="control for routing outcomes"):
        topics.build_backend(control, LABELSET, settings())


def test_a_label_set_is_scored_in_process_only(monkeypatch):
    with pytest.raises(PrerequisiteMissing, match="evaluated in-process"):
        topics.build_backend(CANDIDATE, LABELSET, settings(), via="service")


def test_a_dataset_and_a_label_set_that_disagree_on_the_labels_are_refused(monkeypatch):
    other = type("Other", (), {"labels": {"x": ["a"], "y": ["b"]}})()
    monkeypatch.setattr(topics, "load_labelset", lambda name: other)
    with pytest.raises(PrerequisiteMissing, match="disagree on the labels"):
        topics.run(CANDIDATE, settings(), backend=FakeTopics())


def test_a_dataset_that_is_not_a_topics_dataset_is_refused():
    with pytest.raises(PrerequisiteMissing, match="not 'topics'"):
        topics.run(CANDIDATE, RunSettings(dataset="route-domains", runs=1), backend=FakeTopics())


def test_a_missing_label_set_is_a_missing_prerequisite_not_a_crash(tmp_path):
    with pytest.raises(PrerequisiteMissing, match="no label set named 'nope'"):
        topics.load_labelset("nope", tmp_path)


def test_a_label_set_the_service_would_refuse_is_refused_here_too(tmp_path):
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(PrerequisiteMissing, match="refused by the service's own parser"):
        topics.load_labelset("bad", tmp_path)


def test_the_routing_runner_hands_a_topics_dataset_to_this_runner(monkeypatch):
    seen = {}
    monkeypatch.setattr(topics, "run", lambda candidate, settings, via=None: seen.update(candidate=candidate.name, dataset=settings.dataset, via=via) or "handed over")

    assert classify.run(CANDIDATE, settings(), via="inprocess") == "handed over"
    assert seen == {"candidate": CANDIDATE.name, "dataset": "route-topics", "via": "inprocess"}


# ---- calibration --------------------------------------------------------------------------------------------------------------------
class Scripted:
    """Similarities for dev sentences in order: the in-scope ones get 0.50 .. 0.69 (20 of them), the `none` ones get a fixed list."""

    labels = ("a", "b")
    labelset_version, min_similarity = "v", None
    identity, revision, size_bytes, rss_mb = "s", None, None, None

    def __init__(self, in_scope: list[float], none: list[float]) -> None:
        self.in_scope, self.none = list(in_scope), list(none)

    def nearest(self, texts):
        i_in, i_none, out = 0, 0, []
        for text in texts:
            if text.startswith("none"):
                out.append((0, self.none[i_none])); i_none += 1
            else:
                out.append((0, self.in_scope[i_in])); i_in += 1
        return out


def rows(n_in: int, n_none: int):
    return [{"id": f"i{k}", "text": f"in {k}", "label": "a"} for k in range(n_in)] + [{"id": f"n{k}", "text": f"none {k}", "label": "none"} for k in range(n_none)]


def test_the_cutoff_keeps_the_requested_share_of_in_scope_sentences_and_is_rounded_down():
    similarities = [0.5 + k / 100 for k in range(20)]  # 0.50 .. 0.69
    result = topics.calibrate(Scripted(similarities, [0.1, 0.55, 0.9]), rows(20, 3), "none", keep=0.9)

    # The 10th percentile of 20 values is 0.51 + 0.9 * 0.01 = 0.519 and keeps 18 of 20 (0.52 and up).
    assert result["min_similarity"] == 0.519
    assert result["in_scope_kept"] == pytest.approx(0.9) and result["n_in_scope"] == 20 and result["n_none"] == 3
    assert result["none_rejected"] == pytest.approx(1 / 3)  # only 0.1 is below 0.519; 0.55 and 0.9 are not


def test_calibration_counts_a_none_sentence_as_rejected_only_below_the_cutoff():
    result = topics.calibrate(Scripted([0.6] * 10, [0.1, 0.9, 0.5, 0.7]), rows(10, 4), "none", keep=0.9)

    assert result["min_similarity"] == 0.6
    assert result["none_rejected"] == pytest.approx(0.5)  # 0.1 and 0.5 are below 0.6; 0.9 and 0.7 are not


def test_the_rounding_never_lowers_the_kept_share_below_what_was_asked():
    for keep in (0.5, 0.8, 0.9, 0.95):
        result = topics.calibrate(Scripted(list(np.random.default_rng(3).random(40) * 0.4 + 0.5), []), rows(40, 0), "none", keep=keep)
        assert result["in_scope_kept"] >= keep - 1e-9


@pytest.mark.parametrize("keep", [0.0, 1.0, -0.1, 1.5])
def test_a_keep_share_outside_zero_and_one_is_refused(keep):
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        topics.calibrate(Scripted([0.6], []), rows(1, 0), "none", keep=keep)


def test_a_split_with_no_in_scope_sentence_cannot_be_calibrated():
    with pytest.raises(PrerequisiteMissing, match="no in-scope sentence"):
        topics.calibrate(Scripted([], [0.1]), rows(0, 1), "none")


def test_calibration_reads_only_the_dev_split(monkeypatch):
    seen = {}

    def fake_calibrate(backend, items, none_label, keep):
        seen["texts"] = {r["text"] for r in items}
        return {"min_similarity": 0.5, "in_scope_kept": 1.0, "none_rejected": None, "n_in_scope": len(items), "n_none": 0}

    monkeypatch.setattr(topics, "calibrate", fake_calibrate)
    result = topics.calibration_for(CANDIDATE, settings(), backend=FakeTopics())

    dev = {r["text"] for r in MANIFEST.split_items("dev")}
    test = {r["text"] for r in MANIFEST.split_items("test")}
    assert seen["texts"] == dev and not (seen["texts"] & test), "the cutoff must never be chosen on the reported split"
    assert result["split"] == "dev" and result["calibration_line"] == {"bge-small-en-v1.5": {"min_similarity": 0.5}}


def test_the_calibrate_command_prints_the_line_to_paste(monkeypatch, capsys):
    monkeypatch.setattr(
        topics, "calibration_for",
        lambda candidate, settings, name, keep: {"model": "bge-small-en-v1.5", "min_similarity": 0.7, "in_scope_kept": 0.95, "none_rejected": 0.5,
                                                 "n_in_scope": 48, "n_none": 8, "calibration_line": {"bge-small-en-v1.5": {"min_similarity": 0.7}}},
    )

    assert cli.main(["labelset-calibrate", "--candidate", "labelset-bge-small-en-v1.5"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out[: out.index("\n\nput this")])["min_similarity"] == 0.7
    assert '"bge-small-en-v1.5": {"min_similarity": 0.7}' in out and "route-topics.json" in out
