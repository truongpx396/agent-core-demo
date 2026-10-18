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


def test_the_classifier_declarations_are_the_models_the_service_serves_for_that_role():
    """If the harness evaluated a different revision from the one the service loads, a head would be trained and judged on one model and served on another."""
    from ml_service.registry import load_model_specs

    served = {s.name: s for s in load_model_specs(cli.REPO / "docker" / "ml-service" / "models.toml") if "classify" in s.roles}
    declared = {c.source.model: c for c in cli.declared("classifier") if c.path == "inprocess"}

    assert set(declared) == set(served), "every classify-role model must be declared as a classifier candidate, and nothing else"
    for name, candidate in declared.items():
        spec = served[name]
        assert (candidate.source.repo, candidate.source.file, candidate.source.revision) == (spec.repo, spec.file, spec.revision), name
        assert (candidate.prepare.pooling, candidate.prepare.normalize, candidate.prepare.max_length, candidate.prepare.pad_token) == (
            spec.pooling, spec.normalize, spec.max_length, spec.pad_token), name
        assert candidate.licence == spec.licence, name


def test_the_screen_baseline_is_scored_at_the_apps_own_threshold():
    from scripts.model_eval.runners import screen

    assert screen.app_threshold() == 0.5
