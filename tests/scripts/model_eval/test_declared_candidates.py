"""The candidate declarations committed in evals/candidates/ must be loadable and complete: a typo here would only surface mid-evaluation."""
import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval.candidates import load_candidates


@pytest.mark.parametrize("kind", ["embedding", "reranker", "screen"])
def test_each_kinds_declarations_parse_and_name_exactly_one_baseline(kind):
    declared = cli.declared(kind)
    assert declared, f"evals/candidates/{kind}.toml declares nothing"
    assert len([c for c in declared if c.baseline]) == 1
    assert cli.baseline_candidate(kind).name == next(c.name for c in declared if c.baseline)


def test_every_inprocess_candidate_pins_a_commit_and_names_its_pad_token():
    for kind in ("embedding", "reranker", "screen"):
        for c in cli.declared(kind):
            if c.path == "inprocess":
                assert c.source.revision and len(c.source.revision) == 40, c.name
                assert c.prepare.pad_token, f"{c.name}: no pad_token declared (the harness never guesses one)"


def test_every_candidate_states_a_licence_and_no_name_collides_with_a_control():
    from scripts.model_eval.controls import CONTROL_CANDIDATES

    for kind in ("embedding", "reranker", "screen"):
        controls = set(CONTROL_CANDIDATES[kind])
        for c in cli.declared(kind):
            assert c.licence.strip() and c.name not in controls


def test_the_classifier_file_may_be_empty():
    assert load_candidates(cli.CANDIDATES_DIR / "classifier.toml", expected_kind="classifier") == []


def test_the_screen_baseline_is_scored_at_the_apps_own_threshold():
    from scripts.model_eval.runners import screen

    assert screen.app_threshold() == 0.5
