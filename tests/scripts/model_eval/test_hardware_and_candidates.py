"""The hardware profile and the candidate declarations (scripts/model_eval/hardware.py, candidates.py)."""
import pytest

from scripts.model_eval import hardware
from scripts.model_eval.candidates import load_candidates, parse_candidates

SHA = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


# ---- hardware ---------------------------------------------------------------------------------------------------------------
def test_the_hardware_id_is_stable_for_the_same_machine_and_differs_when_the_thread_count_does():
    a, b, c = hardware.profile(threads=4), hardware.profile(threads=4), hardware.profile(threads=8)
    assert a["id"] == b["id"] and a["id"] != c["id"]
    assert a["id"].startswith("hw-") and len(a["id"]) == 11
    assert {"cpu", "cores", "threads", "onnxruntime", "os"} <= set(a)


def test_resident_memory_is_reported_in_megabytes():
    assert 10 < hardware.rss_mb() < 100_000
    assert hardware.peak_rss_mb() >= 1


# ---- candidates -------------------------------------------------------------------------------------------------------------
def _toml(name="bge-small", kind="embedding", path="inprocess", licence='"MIT"', source=None, prepare="") -> str:
    source = source if source is not None else f'repo = "BAAI/bge-small-en-v1.5"\nfile = "onnx/model.onnx"\nrevision = "{SHA}"'
    lic = f"licence = {licence}\n" if licence is not None else ""
    return f'[[candidate]]\nname = "{name}"\nkind = "{kind}"\npath = "{path}"\n{lic}[candidate.source]\n{source}\n[candidate.prepare]\n{prepare}\n'


def test_a_valid_inprocess_candidate_parses_with_its_preparation():
    (c,) = parse_candidates(_toml(prepare='pooling = "cls"\nmax_length = 512\nquery_prefix = "q: "'))
    assert (c.name, c.kind, c.path, c.licence) == ("bge-small", "embedding", "inprocess", "MIT")
    assert c.source.revision == SHA and c.prepare.query_prefix == "q: " and c.prepare.document_prefix == "" and c.prepare.normalize


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"licence": None}, "licence is required"),
        ({"licence": '"  "'}, "licence is required"),
        ({"name": "Has Space"}, "name must match"),
        ({"kind": "chat"}, "kind must be one of"),
        ({"path": "magic"}, "path must be one of"),
        ({"source": 'repo = "r/r"\nfile = "f.onnx"\nrevision = "main"'}, "full 40-hex commit sha"),
        ({"source": 'repo = "r/r"\nfile = "f.onnx"\nrevision = "5c38ec7c40"'}, "full 40-hex commit sha"),
        ({"source": 'repo = "r/r"'}, "needs source.repo, source.file and source.revision"),
        ({"path": "gateway", "source": 'model = "x"'}, "needs source.alias"),
        ({"path": "service", "source": 'alias = "embed"'}, "needs source.model"),
        ({"prepare": 'pooling = "max"'}, "pooling must be one of"),
        ({"prepare": "max_length = 0"}, "not a plausible context length"),
    ],
)
def test_an_invalid_declaration_is_refused_with_the_reason(kwargs, message):
    with pytest.raises(ValueError, match=message):
        parse_candidates(_toml(**kwargs))


def test_a_gateway_and_a_service_candidate_need_no_revision():
    gateway = parse_candidates(_toml(name="nomic", path="gateway", source='alias = "embed"'))[0]
    service = parse_candidates(_toml(name="svc", path="service", source='model = "bge-small-en-v1.5"'))[0]
    assert gateway.source.alias == "embed" and service.source.model == "bge-small-en-v1.5"


def test_names_are_unique_within_a_file_and_a_file_for_one_kind_refuses_another_kind():
    with pytest.raises(ValueError, match="duplicate name"):
        parse_candidates(_toml() + _toml())
    with pytest.raises(ValueError, match="in a file for"):
        parse_candidates(_toml(kind="reranker"), expected_kind="embedding")


def test_a_missing_declarations_file_declares_nothing(tmp_path):
    assert load_candidates(tmp_path / "nope.toml") == []


def test_at_most_one_candidate_per_file_may_be_the_baseline():
    one = _toml(name="a") .replace('path = "inprocess"', 'path = "inprocess"\nbaseline = true')
    two = one + _toml(name="b").replace('path = "inprocess"', 'path = "inprocess"\nbaseline = true')
    assert parse_candidates(one)[0].baseline is True
    with pytest.raises(ValueError, match="exactly one model in use"):
        parse_candidates(two)


def test_a_busy_machine_gets_a_warning_and_a_quiet_one_does_not(monkeypatch):
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(hardware.os, "getloadavg", lambda: (12.0, 0.0, 0.0))
    assert "the machine was busy" in hardware.load_warning() and "latency figures are inflated" in hardware.load_warning()
    monkeypatch.setattr(hardware.os, "getloadavg", lambda: (2.0, 0.0, 0.0))
    assert hardware.load_warning() is None


def test_no_load_average_on_a_platform_is_not_an_error(monkeypatch):
    def unavailable():
        raise OSError("not supported")

    monkeypatch.setattr(hardware.os, "getloadavg", unavailable)
    assert hardware.load_warning() is None


def test_the_load_that_counts_is_the_load_when_the_run_began_not_the_runs_own(monkeypatch):
    """Regression: the load was read at the END of a run, so a reranker pass that uses every core by design flagged a quiet 12-core machine as
    busy (1-minute load 3.9 before, 9.9 after) and every baseline carried the warning."""
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(hardware.os, "getloadavg", lambda: (9.9, 0.0, 0.0))  # what the run itself left behind

    assert hardware.load_warning(at_start=3.9) is None  # quiet when it began: no warning, whatever the run did to the average
    assert "when the run began" in hardware.load_warning(at_start=14.0)  # busy when it began: warn, with the start figure
