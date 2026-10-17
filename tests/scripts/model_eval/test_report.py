"""The report: assembled from a run and a baseline, validated against specs/014 contracts/model-eval.report.schema.json."""
import json

import numpy as np
import pytest

from scripts.model_eval import baselines
from scripts.model_eval.report import RunOutput, build_report, render_markdown, validate
from scripts.model_eval.suites import EMBEDDING_SUITE, ItemData

HW = {"id": "hw-aaaaaaaa", "cpu": "Test CPU", "cores": 8, "threads": 8, "onnxruntime": "1.29.0", "os": "TestOS 1"}
OTHER_HW = {**HW, "id": "hw-bbbbbbbb"}
N = 120


def _data(scale=1.0, seed=3) -> ItemData:
    rng = np.random.default_rng(seed)
    return ItemData([f"q{i}" for i in range(N)], {m: np.clip((rng.random(N) * 0.5 + 0.4) * scale, 0, 1) for m in EMBEDDING_SUITE.names()})


def _eff(p50=9.0):
    return {"query_ms": {"p50": p50, "p95": p50 * 1.5}, "ms_per_chunk": 14.0, "rss_mb": 300.0, "run_seconds": 42.0}


def _run(data, *, name="bge-small-en-v1.5", reviewed=1.0, version="v1", hw_eff=None, dim=384) -> RunOutput:
    return RunOutput(
        kind="embedding",
        candidate={"name": name, "identity": "BAAI/bge-small-en-v1.5", "licence": "MIT", "path": "inprocess", "dim": dim, "size_bytes": 133_000_000, "revision": "5c38ec7c"},
        dataset={"name": "retrieval-domains", "version": version, "split": "test", "items": N, "reviewed_share": reviewed},
        data=data,
        efficiency=hw_eff or _eff(),
    )


def _baseline(tmp_path, data=None, reviewed=1.0, hw="hw-aaaaaaaa"):
    record = {"kind": "embedding", "candidate": "nomic-embed-text@ollama", "dataset": {"name": "retrieval-domains", "version": "v1", "reviewed_share": reviewed},
              "efficiency": {hw: _eff(13.0)}}
    return baselines.read_baseline(baselines.write_baseline(tmp_path, record, data or _data()))


def _report(run, baseline, hardware=HW):
    return build_report(run, EMBEDDING_SUITE, baseline, hardware=hardware, repo_revision="abc1234", ran_at="2026-10-20T10:00:00Z", n_boot=200)


def test_a_report_against_a_baseline_is_schema_valid_and_ok_when_everything_is_reviewed_and_the_controls_pass(tmp_path):
    report = _report(_run(_data(scale=0.8)), _baseline(tmp_path))

    assert validate(report) == []
    assert report["status"] == "ok" and report["warnings"] == []
    assert set(report["deltas"]) == set(EMBEDDING_SUITE.names())
    assert report["baseline"]["latency_comparable"] is True
    assert all(c["ok"] for c in report["controls"])


def test_a_clearly_worse_candidate_is_reported_worse_in_the_deltas_and_the_tradeoffs(tmp_path):
    report = _report(_run(_data(scale=0.5)), _baseline(tmp_path))

    assert {d["verdict"] for d in report["deltas"].values()} == {"worse"}
    assert report["tradeoffs"][0] == "quality: better on nothing; worse on recall@5, recall@20, mrr@10, ndcg@10; inconclusive on nothing"


def test_an_unreviewed_evaluation_makes_the_report_provisional_and_says_so(tmp_path):
    report = _report(_run(_data(), reviewed=0.25), _baseline(tmp_path, reviewed=0.25))

    assert report["status"] == "provisional" and validate(report) == []
    assert any(w.startswith("PROVISIONAL: 75%") for w in report["warnings"])
    assert any("baseline is PROVISIONAL" in w for w in report["warnings"])


def test_latency_is_not_compared_across_different_hardware_but_quality_still_is(tmp_path):
    report = _report(_run(_data(scale=0.8)), _baseline(tmp_path, hw="hw-aaaaaaaa"), hardware=OTHER_HW)

    assert report["baseline"]["latency_comparable"] is False
    assert report["baseline"]["hw_used"] == "hw-aaaaaaaa"  # which machine the baseline's latency is from, so the reader can see
    assert any("latency is not compared" in w for w in report["warnings"])
    assert report["deltas"]  # quality deltas stand
    assert "no baseline on this hardware" in " ".join(report["tradeoffs"])


def test_a_control_candidate_that_comes_out_equal_to_the_baseline_makes_the_report_invalid(tmp_path):
    # `random-vectors` must be WORSE than the baseline; here it is identical, so either the control or the harness is broken
    report = _report(_run(_data(), name="random-vectors"), _baseline(tmp_path, data=_data()))

    assert report["status"] == "invalid" and validate(report) == []
    assert any(c["name"] == "random-vectors" and not c["ok"] for c in report["controls"])
    assert any(w.startswith("INVALID") for w in report["warnings"])


def test_a_baseline_from_another_dataset_version_is_refused(tmp_path):
    with pytest.raises(baselines.BaselineMismatch, match="different evaluations"):
        _report(_run(_data(), version="v2"), _baseline(tmp_path))


def test_a_report_with_no_baseline_is_still_valid_and_still_carries_controls(tmp_path):
    report = _report(_run(_data()), None)

    assert "baseline" not in report and report["deltas"] == {}
    assert validate(report) == []
    assert report["controls"] and report["tradeoffs"][0].startswith("quality: no baseline")


def test_a_report_carries_ids_and_aggregates_only_never_item_text(tmp_path):
    report = _report(_run(_data()), _baseline(tmp_path))
    text = json.dumps(report)

    assert '"q0"' not in text and "query" not in report  # no per-item content, not even ids: a report is pasted into PRs


def test_the_embedding_tradeoff_states_the_storage_and_the_reembed_requirement(tmp_path):
    line = next(t for t in _report(_run(_data(), dim=384), _baseline(tmp_path))["tradeoffs"] if t.startswith("embedding:"))

    assert "dim 384" in line and "1.5 GB per million chunks" in line and "make reembed" in line


def test_the_schema_rejects_an_ok_status_on_an_unreviewed_set():
    bad = {"schema_version": 1, "kind": "embedding", "status": "ok",
           "candidate": {"name": "x", "identity": "x", "licence": "MIT", "path": "gateway"},
           "dataset": {"name": "d", "version": "v1", "split": "test", "items": 5, "reviewed_share": 0.0},
           "hardware": HW, "repo_revision": "r", "ran_at": "2026-10-20T10:00:00Z",
           "quality": {"ndcg@10": {"value": 0.5, "ci95": [0.4, 0.6]}}, "efficiency": {"query_ms": {"p50": 1.0, "p95": 2.0}},
           "controls": [{"name": "identity", "expect": "no_difference", "observed": "no_difference", "ok": True}],
           "tradeoffs": ["t"], "warnings": []}
    assert validate(bad) != []


def test_the_markdown_view_shows_status_quality_controls_and_tradeoffs(tmp_path):
    text = render_markdown(_report(_run(_data(scale=0.8)), _baseline(tmp_path)))

    assert text.startswith("# embedding evaluation: bge-small-en-v1.5") and "**Status: OK**" in text
    assert "| ndcg@10 |" in text and "## Controls" in text and "## Trade-offs" in text


def test_a_baseline_recorded_on_a_busy_machine_makes_every_comparison_against_it_say_so(tmp_path):
    record = {"kind": "embedding", "candidate": "nomic", "dataset": {"name": "retrieval-domains", "version": "v1", "reviewed_share": 1.0},
              "efficiency": {"hw-aaaaaaaa": _eff(13.0)}, "warnings": ["the machine was busy (1-minute load 14.0 on 12 cores): the latency figures are inflated and noisy"]}
    baseline = baselines.read_baseline(baselines.write_baseline(tmp_path, record, _data()))

    report = _report(_run(_data()), baseline)

    assert any("baseline's latency was recorded on a busy machine" in w for w in report["warnings"])
