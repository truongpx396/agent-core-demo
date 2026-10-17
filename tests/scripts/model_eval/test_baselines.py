"""Baseline records: immutable, hardware-keyed, dataset-checked (scripts/model_eval/baselines.py)."""
import json

import numpy as np
import pytest

from scripts.model_eval import baselines
from scripts.model_eval.suites import ItemData


def _record(reviewed=1.0, version="v1", candidate="nomic") -> dict:
    return {"kind": "embedding", "candidate": candidate, "dataset": {"name": "retrieval-domains", "version": version, "reviewed_share": reviewed},
            "efficiency": {"hw-aaaaaaaa": {"query_ms": {"p50": 9.0, "p95": 12.0}}}}


def _data() -> ItemData:
    return ItemData(["q1", "q2"], {"ndcg@10": np.array([0.5, 0.7])})


def test_a_baseline_round_trips_with_its_per_item_values(tmp_path):
    path = baselines.write_baseline(tmp_path, _record(), _data())
    again = baselines.read_baseline(path)

    assert path == tmp_path / "embedding" / "nomic@retrieval-domains-v1.json"
    assert again.data.ids == ["q1", "q2"] and again.data.arrays["ndcg@10"].tolist() == [0.5, 0.7]
    assert again.record["provisional"] is False and again.record["schema_version"] == 1


def test_a_baseline_is_immutable_unless_forced(tmp_path):
    baselines.write_baseline(tmp_path, _record(), _data())
    with pytest.raises(baselines.BaselineExists, match="immutable"):
        baselines.write_baseline(tmp_path, _record(), _data())
    baselines.write_baseline(tmp_path, _record(), _data(), force=True)


def test_a_baseline_on_an_unreviewed_dataset_is_provisional(tmp_path):
    path = baselines.write_baseline(tmp_path, _record(reviewed=0.4), _data())
    assert baselines.read_baseline(path).record["provisional"] is True


def test_a_baseline_from_another_dataset_version_is_refused_not_warned_about(tmp_path):
    baseline = baselines.read_baseline(baselines.write_baseline(tmp_path, _record(version="v1"), _data()))
    baselines.check_comparable(baseline, "retrieval-domains", "v1")
    with pytest.raises(baselines.BaselineMismatch, match="different evaluations"):
        baselines.check_comparable(baseline, "retrieval-domains", "v2")
    with pytest.raises(baselines.BaselineMismatch):
        baselines.check_comparable(baseline, "other-set", "v1")


def test_a_second_machine_adds_its_latency_without_touching_the_first(tmp_path):
    path = baselines.write_baseline(tmp_path, _record(), _data())
    baselines.merge_efficiency(path, "hw-bbbbbbbb", {"query_ms": {"p50": 30.0, "p95": 40.0}})
    baseline = baselines.read_baseline(path)

    assert baseline.efficiency_for("hw-aaaaaaaa")["query_ms"]["p50"] == 9.0
    assert baseline.efficiency_for("hw-bbbbbbbb")["query_ms"]["p50"] == 30.0
    assert baseline.efficiency_for("hw-cccccccc") is None  # no number for hardware it was never measured on


def test_a_record_from_a_newer_schema_is_refused_rather_than_misread(tmp_path):
    path = baselines.write_baseline(tmp_path, _record(), _data())
    record = json.loads(path.read_text())
    record["schema_version"] = 99
    path.write_text(json.dumps(record))
    with pytest.raises(baselines.BaselineMismatch, match="not understood"):
        baselines.read_baseline(path)


def test_find_returns_none_when_no_baseline_was_recorded(tmp_path):
    assert baselines.find_baseline(tmp_path, "embedding", "nomic", "retrieval-domains", "v1") is None


def test_a_hand_edited_per_item_file_is_detectable_from_the_recorded_hash(tmp_path):
    import hashlib

    path = baselines.write_baseline(tmp_path, _record(), _data())
    record = json.loads(path.read_text())
    items = path.with_suffix(".items.json")
    assert record["per_query_sha256"] == hashlib.sha256(items.read_bytes()).hexdigest()
    items.write_text(items.read_text().replace("0.5", "0.9"))
    assert record["per_query_sha256"] != hashlib.sha256(items.read_bytes()).hexdigest()
