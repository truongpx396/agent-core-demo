"""The candidate declarations committed in evals/candidates/ must be loadable and complete: a typo here would only surface mid-evaluation."""
import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval.candidates import load_candidates


@pytest.mark.parametrize("kind", ["embedding", "reranker", "screen", "classifier"])
def test_each_kinds_declarations_parse_and_name_exactly_one_baseline(kind):
    declared = cli.declared(kind)
    assert declared, f"evals/candidates/{kind}.toml declares nothing"
    assert len([c for c in declared if c.baseline]) == 1
    assert cli.baseline_candidate(kind).name == next(c.name for c in declared if c.baseline)


def test_every_inprocess_candidate_pins_a_commit_and_names_its_pad_token():
    for kind in ("embedding", "reranker", "screen", "classifier"):
        for c in cli.declared(kind):
            if c.path == "inprocess":
                assert c.source.revision and len(c.source.revision) == 40, c.name
                assert c.prepare.pad_token, f"{c.name}: no pad_token declared (the harness never guesses one)"


def test_every_candidate_states_a_licence_and_no_name_collides_with_a_control():
    from scripts.model_eval.controls import CONTROL_CANDIDATES

    for kind in ("embedding", "reranker", "screen", "classifier"):
        controls = set(CONTROL_CANDIDATES[kind])
        for c in cli.declared(kind):
            assert c.licence.strip() and c.name not in controls


def test_a_missing_candidates_file_is_an_empty_declaration_not_an_error(tmp_path):
    assert load_candidates(tmp_path / "nothing.toml", expected_kind="classifier") == []


def test_the_classifier_candidates_are_the_service_path_and_the_label_set_models():
    """The in-process HEAD candidates went with the head trainer; what is declared now is the service path (the baseline) and the two models that
    serve label sets (specs/015). A new router is handed to the runner and gets a declaration when it is wired to a command."""
    declared = cli.declared("classifier")

    assert [(c.name, c.path) for c in declared] == [
        ("ml-service-classify", "service"), ("labelset-bge-small-en-v1.5", "inprocess"), ("labelset-all-minilm-l6-v2", "inprocess"),
        ("rules-and-labels-bge-small-en-v1.5", "inprocess"),
    ]
    assert [c.name for c in declared if c.baseline] == ["ml-service-classify"]


def test_a_label_set_candidate_is_exactly_the_model_the_ml_service_serves_for_that_role():
    """The candidate is how the harness loads the model and models.toml is how the service does. If they drift, the report measures a model the
    service does not run, and the cutoff stored for that name would belong to the other one."""
    import tomllib
    from pathlib import Path

    models = {m["name"]: m for m in tomllib.loads((Path(cli.REPO) / "docker" / "ml-service" / "models.toml").read_text())["model"]}
    candidates = [c for c in cli.declared("classifier") if c.name.startswith(("labelset-", "rules-and-labels-"))]

    assert candidates
    for c in candidates:
        served = models[c.source.model]
        assert "classify" in served["roles"], c.name
        assert (c.source.repo, c.source.file, c.source.revision) == (served["repo"], served["file"], served["revision"]), c.name
        assert (c.prepare.pooling, c.prepare.normalize, c.prepare.max_length, c.prepare.pad_token) == (
            served["pooling"], served.get("normalize", True), served["max_length"], served["pad_token"]), c.name
        assert not c.prepare.query_prefix and not c.prepare.document_prefix, "examples and sentences are the same kind of text: no prefix"


def test_the_screen_baseline_is_scored_at_the_apps_own_threshold():
    from scripts.model_eval.runners import screen

    assert screen.app_threshold() == 0.5
