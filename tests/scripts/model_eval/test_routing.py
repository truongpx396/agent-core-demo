"""scripts/model_eval/routing.py: the outcomes file and the label a head learns. Hermetic (files in tmp_path only)."""
import json

import pytest

from scripts.model_eval import routing
from scripts.model_eval.routing import (
    Outcome,
    OutcomesError,
    label,
    outcomes_sha256,
    read_outcomes,
    write_outcomes,
)


def write_lines(path, *rows):
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))


def test_a_round_trip_keeps_every_row_in_order_and_the_hash_is_of_the_bytes_written(tmp_path):
    rows = [Outcome("b", 0.25, 0.75, "judge"), Outcome("a", None, 0.5, "judge")]
    path = tmp_path / "outcomes" / "strong.jsonl"

    digest = write_outcomes(path, rows)

    assert list(read_outcomes(path).values()) == rows  # order of the dict is the order of the file
    assert digest == outcomes_sha256(path)
    assert path.read_text().splitlines()[0] == '{"id": "b", "judge": "judge", "local": 0.25, "strong": 0.75}'  # sorted keys: stable bytes


def test_a_missing_score_is_null_not_zero_and_is_not_complete(tmp_path):
    path = tmp_path / "o.jsonl"
    write_outcomes(path, [Outcome("a", None, 0.9, "j")])

    (outcome,) = read_outcomes(path).values()

    assert outcome.local is None and not outcome.complete
    assert json.loads(path.read_text())["local"] is None  # the file says "unknown", never 0.0


@pytest.mark.parametrize("bad", [1.2, -0.1, "0.5", True, float("inf")])
def test_a_score_that_is_not_a_number_in_zero_to_one_is_refused(tmp_path, bad):
    path = tmp_path / "o.jsonl"
    path.write_text(json.dumps({"id": "a", "local": bad, "strong": 0.5, "judge": "j"}, allow_nan=True) + "\n")

    with pytest.raises(OutcomesError, match="a score must be null or a number in"):
        read_outcomes(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"local": 0.1, "strong": 0.2, "judge": "j"}, "`id`"),
        ({"id": "a", "local": 0.1, "strong": 0.2}, "`judge`"),
        ({"id": "a", "local": 0.1, "strong": 0.2, "judge": ""}, "`judge`"),
        ([1, 2], "must be a JSON object"),
        ("{not json", "not JSON"),
    ],
)
def test_a_malformed_row_is_refused_with_its_line_number(tmp_path, row, message):
    path = tmp_path / "o.jsonl"
    write_lines(path, {"id": "ok", "local": 0.1, "strong": 0.2, "judge": "j"}, row)

    with pytest.raises(OutcomesError, match=rf"o\.jsonl line 2: .*{message}"):
        read_outcomes(path)


def test_a_repeated_prompt_id_is_refused(tmp_path):
    path = tmp_path / "o.jsonl"
    write_lines(path, {"id": "a", "local": 0.1, "strong": 0.2, "judge": "j"}, {"id": "a", "local": 0.3, "strong": 0.4, "judge": "j"})

    with pytest.raises(OutcomesError, match="appears twice"):
        read_outcomes(path)


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "o.jsonl"
    write_lines(path, "", {"id": "a", "local": 0.1, "strong": 0.2, "judge": "j"}, "   ")

    assert list(read_outcomes(path)) == ["a"]


def test_a_failed_write_leaves_the_previous_file_whole_and_no_temp_file_behind(tmp_path, monkeypatch):
    path = tmp_path / "o.jsonl"
    write_outcomes(path, [Outcome("a", 0.1, 0.9, "j")])
    before = path.read_bytes()
    monkeypatch.setattr(routing.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        write_outcomes(path, [Outcome("a", 0.5, 0.5, "j"), Outcome("b", 0.5, 0.5, "j")])

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["o.jsonl"]


@pytest.mark.parametrize(
    ("local", "strong", "margin", "expected"),
    [
        (0.5, 0.7, 0.2, 1),  # exactly the margin: 0.7 - 0.5 is 0.19999999999999996 in floating point, and must still count
        (0.3, 0.5, 0.2, 1),
        (0.5, 0.69, 0.2, 0),  # one hundredth short
        (0.9, 0.9, 0.2, 0),
        (0.9, 0.5, 0.2, 0),  # the strong model was WORSE: still local_ok
        (0.0, 0.1, 0.1, 1),
        (0.4, 0.5, 0.05, 1),
    ],
)
def test_the_label_is_needs_stronger_iff_the_gap_reaches_the_margin(local, strong, margin, expected):
    assert label(Outcome("a", local, strong, "j"), margin) == expected


def test_the_default_margin_is_the_one_in_the_contract():
    assert routing.DEFAULT_MARGIN == 0.2
    assert label(Outcome("a", 0.5, 0.7, "j")) == 1


def test_an_outcome_with_a_missing_score_cannot_be_labelled():
    with pytest.raises(OutcomesError, match="missing score"):
        label(Outcome("a", None, 0.9, "j"))
