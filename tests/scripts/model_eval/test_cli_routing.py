"""The routing half of the CLI: `route-label`, `head train` and `run --kind classifier`, end to end with fakes (specs/014 T070-T072).

The classifier runner is exercised for real through the two CONTROL candidates (`constant`, `random-router`), which need no model; the gateway
is a fake chat function; the embedder is a fake. Paths are redirected to a temporary copy of the committed route dataset.
"""
import dataclasses
import hashlib
import json
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval import datasets as ds
from scripts.model_eval import head, route_label, routing
from scripts.model_eval.runners import classify
from scripts.model_eval.runners.base import PrerequisiteMissing

SHA = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
TOML = f"""
[[candidate]]
name = "bge-small-en-v1.5"
kind = "classifier"
path = "inprocess"
licence = "MIT"
baseline = true
[candidate.source]
repo = "BAAI/bge-small-en-v1.5"
file = "onnx/model.onnx"
revision = "{SHA}"
model = "bge-small-en-v1.5"
"""
MODEL = head.ModelInfo("bge-small-en-v1.5", "BAAI/bge-small-en-v1.5", "onnx/model.onnx", SHA, 8)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temporary datasets root holding a copy of the committed (verified) route set, an empty outcomes directory and a classifier declaration."""
    root = tmp_path / "datasets"
    # Without `outcomes/`: that directory is gitignored operator state (a real `make router-label` run leaves a file there), and a test that read it would pass
    # in CI and fail on a machine where someone had labelled the prompts.
    shutil.copytree(ds.DATASETS_ROOT / "route-domains", root / "route-domains", ignore=shutil.ignore_patterns("outcomes"))
    (tmp_path / "cand").mkdir()
    (tmp_path / "cand" / "classifier.toml").write_text(TOML)
    monkeypatch.setattr(ds, "DATASETS_ROOT", root)
    monkeypatch.setattr(cli, "CANDIDATES_DIR", tmp_path / "cand")
    monkeypatch.setattr(cli, "BASELINES_DIR", tmp_path / "baselines")
    original = cli._settings
    monkeypatch.setattr(cli, "_settings", lambda args: dataclasses.replace(original(args), dataset_root=root))
    version = root / "route-domains" / "v1"
    manifest = ds.load_verified(version)
    prompts = [r for split in sorted(manifest.raw["splits"]) for r in manifest.split_items(split)]
    return SimpleNamespace(tmp=tmp_path, root=root, version=version, prompts=prompts, monkeypatch=monkeypatch, manifest=manifest)


def write_outcomes(env, *, gap_for_hard=0.6, name="chat-strong.jsonl"):
    """Every prompt scored: the even-numbered ones need the stronger model (a big gap), the odd ones do not."""
    rows = [routing.Outcome(p["id"], 0.3, 0.3 + (gap_for_hard if i % 2 == 0 else 0.05), "judge") for i, p in enumerate(env.prompts)]
    path = routing.outcomes_dir(env.version) / name
    routing.write_outcomes(path, rows)
    return path


def run_args(candidate, *extra):
    return ["run", "--kind", "classifier", "--candidate", candidate, "--runs", "1", "--no-compare", *extra]


# ---- run --kind classifier ---------------------------------------------------------------------------------------------------------
def test_without_outcomes_routing_quality_is_unmeasured_exit_three_and_no_report(env, capsys, tmp_path):
    out = tmp_path / "reports"

    assert cli.main(run_args("constant", "--out", str(out))) == cli.MISSING

    err = capsys.readouterr().err
    assert "UNMEASURED" in err and "make router-label" in err
    assert not out.exists()  # no report: a number computed from nothing would be worse than none


def test_a_strong_model_that_is_no_better_makes_routing_unmeasured_too(env, capsys):
    write_outcomes(env, gap_for_hard=-0.2)  # the strong model is WORSE on the hard prompts and +0.05 on the rest: no gap to recover

    assert cli.main(run_args("constant")) == cli.MISSING
    assert "did not score above the local one" in capsys.readouterr().err


@pytest.mark.parametrize("control", ["constant", "random-router"])
def test_the_control_candidates_run_through_the_real_runner_and_the_derived_controls_pass(env, capsys, tmp_path, control):
    write_outcomes(env)
    out = tmp_path / "reports"

    code = cli.main(run_args(control, "--out", str(out)))

    assert code == cli.OK, capsys.readouterr()
    (report_file,) = out.glob("*.json")
    report = json.loads(report_file.read_text())
    assert report["kind"] == "classifier" and report["candidate"]["name"] == control
    assert set(report["quality"]) >= {"accuracy", "macro_f1", "ece", "apgr", "apgr_vs_length"}  # cpt50/cpt90 are absent when a router never reaches the fraction
    assert [c["name"] for c in report["controls"]] == ["identity", "random-router", "constant"]
    assert all(c["ok"] for c in report["controls"])  # the harness tells a perfect router from a useless one on THIS data
    assert [line.split(":")[0] for line in report["tradeoffs"] if "router" in line.split(":")[0]] == ["this router", "random router", "longest-first router"]
    assert report["dataset"]["name"] == "route-domains" and report["dataset"]["items"] == len(
        [p for p in env.manifest.split_items("test")]
    )


def test_a_useless_router_scores_near_the_random_baseline_in_its_own_numbers(env, tmp_path):
    write_outcomes(env)
    out = tmp_path / "reports"

    cli.main(run_args("random-router", "--out", str(out)))

    report = json.loads(next(out.glob("*.json")).read_text())
    assert report["quality"]["apgr"]["value"] == pytest.approx(0.5, abs=0.15)  # a random router recovers about what it escalates
    assert report["quality"]["accuracy"]["value"] < 0.75


def test_prompts_with_a_missing_score_are_left_out_and_the_report_says_how_many(env, tmp_path):
    path = write_outcomes(env)
    rows = routing.read_outcomes(path)
    test_ids = [r["id"] for r in env.manifest.split_items("test")]
    rows[test_ids[0]] = routing.Outcome(test_ids[0], None, 0.9, "judge")
    routing.write_outcomes(path, rows.values())
    out = tmp_path / "reports"

    cli.main(run_args("constant", "--out", str(out)))

    report = json.loads(next(out.glob("*.json")).read_text())
    assert any("1 of" in w and "missing score" in w for w in report["warnings"])
    assert report["dataset"]["items"] == len(test_ids) - 1


def test_two_outcomes_files_are_ambiguous_and_the_operator_must_say_which(env, capsys):
    write_outcomes(env, name="a.jsonl")
    write_outcomes(env, name="b.jsonl")

    assert cli.main(run_args("constant")) == cli.MISSING
    assert "pass --outcomes" in capsys.readouterr().err


def test_a_named_outcomes_file_is_used_even_when_there_are_several(env, tmp_path):
    write_outcomes(env, name="a.jsonl")
    chosen = write_outcomes(env, name="b.jsonl")

    assert cli.main(run_args("constant", "--out", str(tmp_path / "r"), "--outcomes", str(chosen))) == cli.OK


# ---- the latency budget ------------------------------------------------------------------------------------------------------------
def test_a_p95_over_fifty_milliseconds_adds_the_sc009_warning(env, tmp_path, monkeypatch):
    write_outcomes(env)
    monkeypatch.setattr(classify, "time_ms", lambda fn: (fn(), 75.0)[1])  # every single-prompt call "took" 75 ms
    out = tmp_path / "reports"

    cli.main(run_args("constant", "--out", str(out)))

    report = json.loads(next(out.glob("*.json")).read_text())
    assert any("p95" in w and "50 ms" in w and "SC-009" in w for w in report["warnings"])


def test_a_fast_classifier_gets_no_latency_warning(env, tmp_path, monkeypatch):
    write_outcomes(env)
    monkeypatch.setattr(classify, "time_ms", lambda fn: (fn(), 4.0)[1])
    out = tmp_path / "reports"

    cli.main(run_args("constant", "--out", str(out)))

    assert not any("SC-009" in w for w in json.loads(next(out.glob("*.json")).read_text())["warnings"])


# ---- route-label -------------------------------------------------------------------------------------------------------------------
class FakeGateway:
    def __init__(self, broken=()):
        self.calls, self.broken = [], set(broken)

    def __call__(self, alias, messages, timeout):
        self.calls.append(alias)
        if alias in self.broken:
            raise ConnectionError("refused")
        if len(messages) == 1 and "ready" in messages[0]["content"]:  # the CLI's preflight ping, sent to every alias
            return "ready"
        if alias == "the-judge":
            return '{"score": 0.9}' if "STRONG" in messages[1]["content"] else '{"score": 0.4}'
        return ("STRONG" if alias == "chat-strong" else "LOCAL") + " answer"


def label_args(*extra):
    return ["route-label", "--judge", "the-judge", "--limit", "3", *extra]


def test_route_label_writes_outcomes_and_a_sidecar_and_a_rerun_does_nothing_more(env, capsys, monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(route_label, "gateway_chat", lambda: gateway)

    assert cli.main(label_args()) == cli.OK

    outcomes = routing.read_outcomes(routing.outcomes_dir(env.version) / "chat-strong.jsonl")
    assert len(outcomes) == 3 and all((o.local, o.strong, o.judge) == (0.4, 0.9, "the-judge") for o in outcomes.values())
    meta = json.loads((routing.outcomes_dir(env.version) / "chat-strong.meta.json").read_text())
    assert meta["judge"] == "the-judge" and meta["dataset"]["name"] == "route-domains" and meta["last_run"]["scored"] == 3
    before = len(gateway.calls)
    assert cli.main(label_args()) == cli.OK
    assert "skipped 3 already done" in capsys.readouterr().out
    assert len(gateway.calls) == before + 3  # only the three preflight pings: no prompt was asked about again


def test_route_label_stops_before_spending_time_when_an_alias_is_unreachable(env, capsys, monkeypatch):
    monkeypatch.setattr(route_label, "gateway_chat", lambda: FakeGateway(broken={"chat-strong"}))

    assert cli.main(label_args()) == cli.MISSING

    assert "chat-strong" in capsys.readouterr().err
    assert not (routing.outcomes_dir(env.version) / "chat-strong.jsonl").exists()


def test_route_label_uses_the_strong_id_as_the_file_name(env, monkeypatch):
    monkeypatch.setattr(route_label, "gateway_chat", lambda: FakeGateway())

    assert cli.main(label_args("--strong-id", "qwen2.5-7b")) == cli.OK
    assert (routing.outcomes_dir(env.version) / "qwen2.5-7b.jsonl").exists()


# ---- head train --------------------------------------------------------------------------------------------------------------------
def parity(text):
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 2


def fake_embed(texts):
    out = []
    for text in texts:
        rng = np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[8:16], 16))
        v = rng.normal(size=8) * 0.2
        v[0] += 3.0 if parity(text) else -3.0
        out.append(v)
    return np.array(out)


def write_separable_outcomes(env):
    by_id = {p["id"]: p["text"] for p in env.prompts}
    rows = [routing.Outcome(i, 0.3, 0.9 if parity(t) else 0.32, "judge") for i, t in by_id.items()]
    path = routing.outcomes_dir(env.version) / "chat-strong.jsonl"
    routing.write_outcomes(path, rows)
    return path


def train_args(env, *extra):
    return ["head", "train", "--model", "bge-small-en-v1.5", "--out", str(env.tmp / "heads"), *extra]


def test_head_train_writes_an_unaccepted_head_and_says_a_person_must_accept_it(env, capsys, monkeypatch):
    write_separable_outcomes(env)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))

    assert cli.main(train_args(env)) == cli.OK

    out = capsys.readouterr().out
    doc = json.loads((env.tmp / "heads" / "head-bge-small-en-v1.5.json").read_text())
    assert doc["accepted"] is False and "accepted: false" in out and "a person's decision" in out
    assert doc["trained_on"]["dataset"] == "route-domains" and doc["trained_on"]["judge"] == "judge" and doc["heldout"]["accuracy"] > 0.9


def test_head_train_without_outcomes_exits_three_and_names_the_command_that_makes_them(env, capsys):
    assert cli.main(train_args(env)) == cli.MISSING
    assert "make router-label" in capsys.readouterr().err


def test_head_train_with_one_label_everywhere_is_refused_with_exit_two(env, capsys, monkeypatch):
    rows = [routing.Outcome(p["id"], 0.3, 0.31, "judge") for p in env.prompts]  # the strong model never helps by the margin
    routing.write_outcomes(routing.outcomes_dir(env.version) / "chat-strong.jsonl", rows)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))

    assert cli.main(train_args(env)) == cli.USAGE
    assert "same label" in capsys.readouterr().err
    assert not (env.tmp / "heads").exists()


def test_a_contaminated_training_set_is_a_verification_failure_exit_four(env, capsys, monkeypatch):
    write_separable_outcomes(env)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))

    def refuse(**_kwargs):
        raise head.ContaminatedTrainingSet("the training prompts overlap the held-out split")

    monkeypatch.setattr(head, "train_head", refuse)

    assert cli.main(train_args(env)) == cli.VERIFY_FAILED


def test_head_train_will_not_replace_an_accepted_head_without_force(env, capsys, monkeypatch):
    write_separable_outcomes(env)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))
    assert cli.main(train_args(env)) == cli.OK
    path = env.tmp / "heads" / "head-bge-small-en-v1.5.json"
    doc = json.loads(path.read_text())
    doc["accepted"] = True
    path.write_text(json.dumps(doc))

    assert cli.main(train_args(env)) == cli.USAGE
    assert json.loads(path.read_text())["accepted"] is True
    assert cli.main(train_args(env, "--force")) == cli.OK
    assert json.loads(path.read_text())["accepted"] is False


def test_the_head_command_needs_a_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["head"])
    assert exc.value.code == 2


def test_a_head_the_service_would_refuse_is_refused_in_process_too(env, tmp_path, monkeypatch):
    """The in-process evaluation must not score with a head bound to another revision: the app could never get those scores."""
    write_separable_outcomes(env)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))
    cli.main(train_args(env))
    other = head.ModelInfo(MODEL.name, MODEL.repo, MODEL.file, "f" * 40, MODEL.dim)
    monkeypatch.setattr(classify, "service_embedder", lambda name: (fake_embed, other))
    candidate = cli.resolve_candidate("classifier", "bge-small-en-v1.5")

    with pytest.raises(PrerequisiteMissing, match="not trained for the model just loaded"):
        classify.InProcessClassifier(candidate, env.tmp / "heads")


def test_an_in_process_run_scores_with_the_head_file_and_warns_that_it_is_not_accepted(env, tmp_path, monkeypatch):
    write_separable_outcomes(env)
    monkeypatch.setattr(head, "service_embedder", lambda name: (fake_embed, MODEL))
    cli.main(train_args(env))
    monkeypatch.setattr(classify, "service_embedder", lambda name: (fake_embed, MODEL))
    out = tmp_path / "reports"

    code = cli.main(run_args("bge-small-en-v1.5", "--via", "inprocess", "--head-dir", str(env.tmp / "heads"), "--out", str(out)))

    assert code == cli.OK
    report = json.loads(next(out.glob("*.json")).read_text())
    assert report["quality"]["accuracy"]["value"] > 0.9 and report["quality"]["apgr"]["value"] > 0.5
    assert any("NOT accepted" in w for w in report["warnings"])
    assert report["candidate"]["identity"].startswith("head:")


def write_noisy_outcomes(env, shift=0.03):
    """The real situation: both models score about the same, with judge noise on a coarse scale, so the strong model's mean advantage is tiny."""
    rng = np.random.default_rng(1)
    scale = [0.5, 0.7, 0.8, 0.9, 1.0]
    rows = [routing.Outcome(p["id"], float(rng.choice(scale)), float(np.clip(rng.choice(scale) + shift, 0, 1)), "judge") for p in env.prompts]
    routing.write_outcomes(routing.outcomes_dir(env.version) / "chat-strong.jsonl", rows)


def test_outcomes_that_cannot_separate_the_models_make_the_report_invalid_and_say_why(env, tmp_path, capsys):
    """Found by the first real labelling run (the strong model beat the local one by +0.033 on a judge's coarse scale): the controls could not tell
    a perfect router from a random one, and the report said only "a control did not come out as expected". It must say it is the outcomes."""
    write_noisy_outcomes(env)
    out = tmp_path / "reports"

    code = cli.main(run_args("constant", "--out", str(out)))

    report = json.loads(next(out.glob("*.json")).read_text())
    assert code == cli.CONTROL_FAILED and report["status"] == "invalid"
    assert any(c["name"] in ("random-router", "constant") and not c["ok"] for c in report["controls"])
    why = [w for w in report["warnings"] if w.startswith("WHY:")]
    assert len(why) == 1 and "cannot tell even a PERFECT router from a random one" in why[0] and "property of the labelled outcomes" in why[0]


def test_outcomes_that_do_separate_the_models_get_no_such_warning(env, tmp_path):
    write_outcomes(env)  # a gap of 0.6 on half the prompts
    out = tmp_path / "reports"

    assert cli.main(run_args("constant", "--out", str(out))) == cli.OK

    assert not any(w.startswith("WHY:") for w in json.loads(next(out.glob("*.json")).read_text())["warnings"])
