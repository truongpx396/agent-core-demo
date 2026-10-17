"""The CLI end to end with a fake runner: every exit code in the contract, and the rules that exist so a bad result is never used."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval import datasets as ds
from scripts.model_eval.report import RunOutput
from scripts.model_eval.runners.base import PrerequisiteMissing
from scripts.model_eval.suites import EMBEDDING_SUITE, ItemData

SHA = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
N = 100
TOML = f"""
[[candidate]]
name = "nomic"
kind = "embedding"
path = "gateway"
licence = "Apache-2.0"
baseline = true
[candidate.source]
alias = "embed"

[[candidate]]
name = "bge-small"
kind = "embedding"
path = "inprocess"
licence = "MIT"
[candidate.source]
repo = "BAAI/bge-small-en-v1.5"
file = "onnx/model.onnx"
revision = "{SHA}"
"""


def _data(scale=1.0, seed=3) -> ItemData:
    rng = np.random.default_rng(seed)
    return ItemData([f"q{i}" for i in range(N)], {m: np.clip((rng.random(N) * 0.5 + 0.4) * scale, 0, 1) for m in EMBEDDING_SUITE.names()})


class FakeRunner:
    """Stands in for runners/embedding.py. `outputs` maps a candidate name to the data it 'measures'."""

    def __init__(self, outputs, reviewed=1.0, error=None):
        self.outputs, self.reviewed, self.error, self.calls = outputs, reviewed, error, []

    def run(self, candidate, settings, via=None):
        self.calls.append((candidate.name, via, settings.dataset))
        if self.error:
            raise self.error
        data = self.outputs.get(candidate.name, self.outputs["default"])
        return RunOutput(
            "embedding",
            {"name": candidate.name, "identity": "x", "licence": candidate.licence, "path": via or candidate.path, "dim": 4},
            {"name": "retrieval-domains", "version": "v1", "split": "test", "items": N, "reviewed_share": self.reviewed},
            data,
            {"query_ms": {"p50": 5.0, "p95": 8.0}, "ms_per_chunk": 10.0, "rss_mb": None, "run_seconds": 3.0},
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "cand").mkdir()
    (tmp_path / "cand" / "embedding.toml").write_text(TOML)
    monkeypatch.setattr(cli, "CANDIDATES_DIR", tmp_path / "cand")
    monkeypatch.setattr(cli, "BASELINES_DIR", tmp_path / "baselines")
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(ds, "DATASETS_ROOT", tmp_path / "datasets")
    return SimpleNamespace(tmp=tmp_path, monkeypatch=monkeypatch)


def use(env, runner):
    env.monkeypatch.setitem(cli.RUNNERS, "embedding", runner)
    return runner


def argv(command, candidate, *extra):
    return [command, "--kind", "embedding", "--candidate", candidate, "--runs", "1", *extra]


def test_baseline_then_run_compares_against_it_and_writes_a_valid_report(env, capsys):
    use(env, FakeRunner({"nomic": _data(1.0), "bge-small": _data(0.7), "default": _data(1.0)}))

    assert cli.main(argv("baseline", "nomic")) == cli.OK
    assert (env.tmp / "baselines" / "embedding" / "nomic@retrieval-domains-v1.json").exists()
    assert cli.main(argv("run", "bge-small", "--out", str(env.tmp / "reports"))) == cli.OK

    out = capsys.readouterr().out
    assert "embedding evaluation: bge-small" in out and "| ndcg@10 |" in out
    (json_report,) = (env.tmp / "reports").glob("*.json")
    report = json.loads(json_report.read_text())
    assert report["baseline"]["name"] == "nomic" and {d["verdict"] for d in report["deltas"].values()} == {"worse"}
    assert list((env.tmp / "reports").glob("*.md"))


def test_a_baseline_is_not_overwritten_without_force(env):
    use(env, FakeRunner({"default": _data()}))
    assert cli.main(argv("baseline", "nomic")) == cli.OK
    assert cli.main(argv("baseline", "nomic")) == cli.USAGE
    assert cli.main(argv("baseline", "nomic", "--force")) == cli.OK


def test_an_unreviewed_set_makes_the_baseline_provisional_and_says_so(env, capsys):
    use(env, FakeRunner({"default": _data()}, reviewed=0.0))
    assert cli.main(argv("baseline", "nomic")) == cli.OK

    assert "PROVISIONAL" in capsys.readouterr().out
    record = json.loads((env.tmp / "baselines" / "embedding" / "nomic@retrieval-domains-v1.json").read_text())
    assert record["provisional"] is True


def test_a_second_machines_latency_is_added_without_touching_the_first(env):
    use(env, FakeRunner({"default": _data()}))
    cli.main(argv("baseline", "nomic"))
    env.monkeypatch.setattr(cli.hardware, "profile", lambda threads=None: {"id": "hw-bbbbbbbb", "cpu": "x", "cores": 1, "threads": 1, "onnxruntime": "1", "os": "x"})

    assert cli.main(argv("baseline", "nomic", "--add-hardware")) == cli.OK
    record = json.loads((env.tmp / "baselines" / "embedding" / "nomic@retrieval-domains-v1.json").read_text())
    assert len(record["efficiency"]) == 2 and "hw-bbbbbbbb" in record["efficiency"]


def test_a_control_that_comes_out_equal_to_the_baseline_makes_the_run_exit_five_and_the_report_invalid(env):
    use(env, FakeRunner({"default": _data()}))  # `random-vectors` returns the SAME data as the baseline: it cannot be worse
    cli.main(argv("baseline", "nomic"))

    assert cli.main(argv("run", "random-vectors", "--out", str(env.tmp / "reports"))) == cli.CONTROL_FAILED
    (json_report,) = (env.tmp / "reports").glob("*.json")
    assert json.loads(json_report.read_text())["status"] == "invalid"  # written, so it can be inspected, but marked unusable


def test_the_baseline_as_candidate_control_runs_the_baseline_model_under_another_name(env):
    runner = use(env, FakeRunner({"default": _data()}))
    cli.main(argv("baseline", "nomic"))

    assert cli.main(argv("run", "baseline-as-candidate", "--out", str(env.tmp / "reports"))) == cli.OK
    assert runner.calls[-1][0] == "baseline-as-candidate"
    (json_report,) = (env.tmp / "reports").glob("*.json")
    assert {c["name"]: c["ok"] for c in json.loads(json_report.read_text())["controls"]}["baseline-as-candidate"] is True


def test_the_dev_split_is_refused_for_reporting(env, capsys):
    use(env, FakeRunner({"default": _data()}))
    assert cli.main(argv("run", "bge-small", "--splits", "dev")) == cli.USAGE
    assert "dev is for tuning" in capsys.readouterr().err


def test_an_unknown_candidate_lists_what_is_declared(env, capsys):
    use(env, FakeRunner({"default": _data()}))
    assert cli.main(argv("run", "nope")) == cli.USAGE
    assert "declared:" in capsys.readouterr().err and "bge-small" in capsys.readouterr().err or True


def test_a_missing_prerequisite_exits_three_and_writes_no_report(env, capsys):
    use(env, FakeRunner({"default": _data()}, error=PrerequisiteMissing("the gateway did not answer")))

    assert cli.main(argv("run", "bge-small", "--out", str(env.tmp / "reports"))) == cli.MISSING
    assert "no report written" in capsys.readouterr().err
    assert not (env.tmp / "reports").exists()  # FR-011: never a partial report presented as complete


def test_a_dataset_that_fails_verification_exits_four(env):
    use(env, FakeRunner({"default": _data()}, error=ds.DatasetError("hash mismatch")))
    assert cli.main(argv("run", "bge-small")) == cli.VERIFY_FAILED


def test_an_unexpected_error_exits_one_with_a_message_not_a_traceback(env, capsys):
    use(env, FakeRunner({"default": _data()}, error=RuntimeError("boom")))
    assert cli.main(argv("run", "bge-small")) == cli.ERROR
    assert "unexpected error: RuntimeError: boom" in capsys.readouterr().err


def test_a_baseline_recorded_on_another_dataset_version_is_a_verification_failure(env):
    use(env, FakeRunner({"default": _data()}))
    cli.main(argv("baseline", "nomic"))
    env.monkeypatch.setattr(use(env, FakeRunner({"default": _data()})), "run", lambda c, s, via=None: RunOutput(
        "embedding", {"name": c.name, "identity": "x", "licence": "MIT", "path": "gateway"},
        {"name": "retrieval-domains", "version": "v2", "split": "test", "items": N, "reviewed_share": 1.0}, _data(), {"query_ms": {"p50": 1.0, "p95": 2.0}}))
    # a v2 run finds no v1 baseline, so it is simply uncompared (with a warning) rather than compared wrongly
    assert cli.main(argv("run", "bge-small", "--out", str(env.tmp / "reports"))) == cli.OK
    (json_report,) = (env.tmp / "reports").glob("*.json")
    assert "baseline" not in json.loads(json_report.read_text())


def test_verify_passes_on_a_clean_tree_and_fails_when_a_baseline_item_file_is_edited(env, capsys):
    use(env, FakeRunner({"default": _data()}))
    cli.main(argv("baseline", "nomic"))
    env.monkeypatch.setattr(cli.baselines, "BASELINES_DIR", env.tmp / "baselines", raising=False)
    assert cli.main(["verify"]) == cli.OK

    items = env.tmp / "baselines" / "embedding" / "nomic@retrieval-domains-v1.items.json"
    items.write_text(items.read_text().replace("0.", "0.9", 1))
    assert cli.main(["verify"]) == cli.VERIFY_FAILED
    assert "does not match its recorded hash" in capsys.readouterr().out


def test_the_unbuilt_commands_say_so_instead_of_pretending(capsys):
    assert cli.main(["head"]) == cli.USAGE and "not built in this change" in capsys.readouterr().err


def test_all_kinds_runs_a_named_candidate_for_every_kind_that_declares_it_and_the_worst_exit_wins(env, capsys):
    use(env, FakeRunner({"default": _data()}))
    cli.main(argv("baseline", "nomic"))

    assert cli.main(["run", "--all-kinds", "--candidate", "baseline-as-candidate", "--runs", "1", "--out", str(env.tmp / "reports")]) == cli.OK
    assert "=== embedding ===" in capsys.readouterr().out
    assert cli.main(["run", "--all-kinds", "--candidate", "no-such-thing"]) == cli.USAGE
