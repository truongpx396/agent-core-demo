"""The harness's controls on the REAL datasets and a REAL ML service (specs/014 T043, SC-002, SC-003).

The hermetic tests prove the metrics and the comparison with synthetic data. This proves the same plumbing end to end where it counts: the
committed repository datasets, the real runner code and a real model behind it. For each kind it records a throwaway baseline into a
temporary directory (it must not depend on the committed baselines, which are recorded later and on someone's machine), then checks:
  * the baseline model run again under another name is NOT distinguishable from the baseline (never `better` or `worse`);
  * a deliberately useless model (random vectors / random scores / a screen that flags nothing) is reported `worse` on the metrics that
    mean "it works", and the control check passes;
  * re-running the same model reproduces every quality number EXACTLY (SC-002).

The embedding leg loads a small ONNX model from the Hugging Face Hub in this process; if the Hub is unreachable that leg is skipped, never
failed. Skips (never fails) without Docker, like every `integration` test.
"""
import json
from pathlib import Path

import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval.candidates import parse_candidates
from scripts.model_eval.runners import embedding, rerank, screen
from scripts.model_eval.runners.base import RunSettings

pytestmark = pytest.mark.integration

EMBED_TOML = """
[[candidate]]
name = "minilm"
kind = "embedding"
path = "inprocess"
licence = "Apache-2.0"
baseline = true
[candidate.source]
repo = "sentence-transformers/all-MiniLM-L6-v2"
file = "onnx/model.onnx"
revision = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
[candidate.prepare]
pooling = "mean"
normalize = true
max_length = 256
pad_token = "[PAD]"
"""
SERVICE_TOML = """
[[candidate]]
name = "service-model"
kind = "{kind}"
path = "service"
licence = "Apache-2.0"
baseline = true
[candidate.source]
model = "ml-service"
"""


@pytest.fixture
def harness(tmp_path, monkeypatch, ml_service_url):
    monkeypatch.setattr("app.core.config.ML_SERVICE_URL", ml_service_url)
    monkeypatch.setattr(cli, "BASELINES_DIR", tmp_path / "baselines")
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "reports")
    cand = tmp_path / "candidates"
    cand.mkdir()
    monkeypatch.setattr(cli, "CANDIDATES_DIR", cand)

    def declare(kind: str, toml: str) -> None:
        (cand / cli.CANDIDATE_FILES[kind]).write_text(toml)

    return declare, tmp_path


def _run(args: list[str]) -> int:
    code = cli.main(args)
    if code == cli.MISSING:
        pytest.skip("a prerequisite is unavailable here (model download or dataset), so this leg is skipped, not failed")
    return code


def _report(tmp_path: Path) -> dict:
    newest = max((tmp_path / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text())


@pytest.mark.parametrize(
    ("kind", "worse_control", "toml"),
    [
        ("reranker", "random-scores", SERVICE_TOML.format(kind="reranker")),
        ("screen", "always-benign", SERVICE_TOML.format(kind="screen")),
        ("embedding", "random-vectors", EMBED_TOML),
    ],
)
def test_the_controls_behave_on_the_real_datasets(harness, kind, worse_control, toml):
    declare, tmp_path = harness
    declare(kind, toml)
    base = ["--kind", kind, "--runs", "1"]
    name = cli.baseline_candidate(kind).name

    assert _run(["baseline", *base, "--candidate", name]) == cli.OK

    assert _run(["run", *base, "--candidate", "baseline-as-candidate", "--out", str(tmp_path / "reports")]) == cli.OK
    same = _report(tmp_path)
    assert {d["verdict"] for d in same["deltas"].values()} <= {"no_difference", "inconclusive"}, "the baseline model was reported as different from itself"
    assert all(c["ok"] for c in same["controls"])

    assert _run(["run", *base, "--candidate", worse_control, "--out", str(tmp_path / "reports")]) == cli.OK  # exit 5 would mean a control failed
    useless = _report(tmp_path)
    assert all(c["ok"] for c in useless["controls"]), useless["controls"]
    assert "worse" in {d["verdict"] for d in useless["deltas"].values()}


def test_rerunning_a_deterministic_model_reproduces_every_quality_number_exactly(ml_service_url, monkeypatch):
    monkeypatch.setattr("app.core.config.ML_SERVICE_URL", ml_service_url)
    candidate = parse_candidates(SERVICE_TOML.format(kind="reranker"))[0]
    settings = RunSettings(runs=1)

    a = rerank.run(candidate, settings)
    b = rerank.run(candidate, settings)

    assert a.data.same_values_as(b.data)  # SC-002: quality is exact; only latency moves


def test_the_screen_is_scored_at_the_apps_threshold_and_reports_the_in_domain_false_positive_rate(ml_service_url, monkeypatch):
    monkeypatch.setattr("app.core.config.ML_SERVICE_URL", ml_service_url)
    candidate = parse_candidates(SERVICE_TOML.format(kind="screen"))[0]

    out = screen.run(candidate, RunSettings(runs=1))

    assert out.data.arrays["label"].sum() > 0 and (out.data.arrays["label"] == 0).sum() > 0  # both classes were scored
    assert set(out.data.arrays) == {"score", "label", "domain", "pattern_hit"}
    assert out.dataset["name"] == "screen-domains"


def test_the_inprocess_embedding_backend_loads_a_real_model_and_embeds_to_the_expected_dimension():
    candidate = parse_candidates(EMBED_TOML)[0]
    try:
        backend = embedding.build_backend(candidate, RunSettings())
    except Exception as exc:  # noqa: BLE001 - the Hub being unreachable is a skip, anything else a failure
        if type(exc).__name__ == "PrerequisiteMissing":
            pytest.skip(str(exc))
        raise
    vectors = backend.embed_documents(["a short passage about refunds"])
    assert vectors.shape == (1, 384) and abs(float((vectors**2).sum()) - 1.0) < 1e-3  # unit length: the model said normalize
