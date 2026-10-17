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
