"""scripts/model_eval/route_label.py with fake models and a fake judge: no gateway, no model, no network (specs/014 T071)."""
import json

import pytest

from scripts.model_eval import route_label
from scripts.model_eval.route_label import label_prompts, parse_score
from scripts.model_eval.routing import (
    Outcome,
    OutcomesError,
    read_outcomes,
    write_outcomes,
)

LOCAL, STRONG, JUDGE = "chat", "chat-strong", "judge-model"
PROMPTS = [{"id": f"p{i}", "text": f"question number {i}"} for i in range(1, 4)]


class Fakes:
    """A fake gateway. Answers are `LOCAL-ANSWER:<question>` / `STRONG-ANSWER:<question>`; the judge reads them back out of its own prompt and
    looks the score up in `table[(who, question)]`, so a test states the scores it expects instead of computing them."""

    def __init__(self, table):
        self.table, self.calls, self.judge_prompts, self.timeouts = table, [], [], []
        self.answer_errors: dict[tuple[str, str], Exception] = {}
        self.judge_replies: dict[tuple[str, str], list[str]] = {}

    def __call__(self, alias, messages, timeout):
        self.calls.append(alias)
        self.timeouts.append(timeout)
        if alias == JUDGE:
            self.judge_prompts.append(messages)
            answer_line = messages[1]["content"].split("ANSWER:\n", 1)[1]
            who, question = answer_line.split("-ANSWER:", 1)
            queued = self.judge_replies.get((who.lower(), question))
            if queued:
                return queued.pop(0)
            return json.dumps({"score": self.table[(who.lower(), question)]})
        question = messages[0]["content"]
        who = "local" if alias == LOCAL else "strong"
        if (who, question) in self.answer_errors:
            raise self.answer_errors[(who, question)]
        return f"{who.upper()}-ANSWER:{question}"


def full_table(local=0.2, strong=0.8):
    return {(who, p["text"]): value for p in PROMPTS for who, value in (("local", local), ("strong", strong))}


def run(fakes, path, **kw):
    return label_prompts(PROMPTS, local=LOCAL, strong=STRONG, judge=JUDGE, chat=fakes, out_path=path, **kw)


# ---- the judge's reply: strict about what counts as a score ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"score": 0.7}', 0.7),
        ('{"score": 1}', 1.0),
        ('{"score": 0, "reason": "wrong"}', 0.0),  # a reason beside the score is still an unambiguous score
        ('```json\n{"score": 0.25}\n```', 0.25),
        ("0.5", 0.5),
        (".5", 0.5),
        (" 1 ", 1.0),
    ],
)
def test_a_score_is_read_from_json_or_a_bare_number(reply, expected):
    assert parse_score(reply) == expected


@pytest.mark.parametrize(
    "reply",
    ["", "I'd say 0.8 out of 10", "score: 0.8", "The answer deserves 0.9.", '{"score": 1.5}', '{"score": -0.1}', '{"score": "0.5"}',
     '{"score": true}', '{"grade": 0.5}', "NaN", "[0.5]", "7", "0.5 0.6"],
)
def test_anything_that_is_not_unambiguously_a_score_is_not_one(reply):
    assert parse_score(reply) is None


# ---- the run ------------------------------------------------------------------------------------------------------------------------
def test_every_prompt_gets_both_scores_in_prompt_order_with_the_judges_alias(tmp_path):
    path = tmp_path / "o" / "strong.jsonl"

    result = run(Fakes(full_table(0.2, 0.8)), path)

    rows = list(read_outcomes(path).values())
    assert [r.id for r in rows] == ["p1", "p2", "p3"]
    assert all((r.local, r.strong, r.judge) == (0.2, 0.8, JUDGE) for r in rows)
    assert (result.scored, result.skipped, result.missing, result.stopped_early) == (3, 0, 0, False)


def test_the_judge_sees_the_question_and_one_answer_and_not_who_wrote_it(tmp_path):
    fakes = Fakes(full_table())

    run(fakes, tmp_path / "o.jsonl")

    assert len(fakes.judge_prompts) == 6  # two answers per prompt, scored separately
    for system, user in fakes.judge_prompts:
        assert system["content"] == route_label.RUBRIC
        text = user["content"]
        assert text.count("ANSWER:\n") == 1 and "QUESTION:\nquestion number" in text
        assert LOCAL not in text and STRONG not in text and JUDGE not in text  # not which model, not which judge
        assert not ("LOCAL-ANSWER" in text and "STRONG-ANSWER" in text)  # never both answers: one at a time


def test_every_call_gets_the_per_call_timeout(tmp_path):
    fakes = Fakes(full_table())

    run(fakes, tmp_path / "o.jsonl", call_timeout=12.5)

    assert fakes.timeouts and set(fakes.timeouts) == {12.5}


def test_a_malformed_judge_reply_is_retried_once_and_the_retry_is_used(tmp_path):
    fakes = Fakes(full_table(0.2, 0.8))
    fakes.judge_replies[("local", "question number 1")] = ["I think it is pretty good"]  # first reply unusable, then the table's 0.2

    result = run(fakes, tmp_path / "o.jsonl")

    assert read_outcomes(tmp_path / "o.jsonl")["p1"].local == 0.2
    assert result.failures == {"judge-malformed": 1} and result.missing == 0


def test_a_judge_that_fails_twice_leaves_the_score_missing_not_zero(tmp_path):
    fakes = Fakes(full_table(0.2, 0.8))
    fakes.judge_replies[("strong", "question number 2")] = ["no idea", "still no idea"]

    result = run(fakes, tmp_path / "o.jsonl")

    row = read_outcomes(tmp_path / "o.jsonl")["p2"]
    assert row.local == 0.2 and row.strong is None  # null, never 0.0
    assert not row.complete and result.missing == 1 and result.failures["judge-malformed"] == 2


def test_a_model_that_times_out_is_missing_and_the_run_goes_on(tmp_path):
    fakes = Fakes(full_table())
    fakes.answer_errors[("strong", "question number 1")] = TimeoutError("slow")

    result = run(fakes, tmp_path / "o.jsonl")

    rows = read_outcomes(tmp_path / "o.jsonl")
    assert rows["p1"].strong is None and rows["p1"].local is not None
    assert rows["p2"].complete and rows["p3"].complete  # one slow answer did not stop the rest
    assert result.failures == {"answer-error:TimeoutError": 1}


def test_a_rerun_skips_finished_prompts_and_retries_the_missing_ones(tmp_path):
    path = tmp_path / "o.jsonl"
    write_outcomes(path, [Outcome("p1", 0.1, 0.9, JUDGE), Outcome("p2", None, 0.9, JUDGE)])
    fakes = Fakes(full_table(0.3, 0.6))

    result = run(fakes, path)

    rows = read_outcomes(path)
    assert (rows["p1"].local, rows["p1"].strong) == (0.1, 0.9)  # untouched: it was finished
    assert (rows["p2"].local, rows["p2"].strong) == (0.3, 0.6)  # retried and now complete
    assert rows["p3"].complete
    assert (result.skipped, result.scored) == (1, 2)
    assert all("question number 1" not in p[1]["content"] for p in fakes.judge_prompts)  # p1 was not asked about again


def test_a_run_killed_between_prompts_leaves_every_finished_prompt_on_disk(tmp_path):
    path = tmp_path / "o.jsonl"
    fakes = Fakes(full_table())
    fakes.answer_errors[("local", "question number 3")] = KeyboardInterrupt()  # not an Exception: the run really dies here

    with pytest.raises(KeyboardInterrupt):
        run(fakes, path)

    assert list(read_outcomes(path)) == ["p1", "p2"]


def test_the_deadline_stops_new_calls_and_what_was_done_is_kept(tmp_path):
    ticks = iter(range(0, 1000))  # the fake clock advances one second per reading
    fakes = Fakes(full_table())

    result = run(fakes, tmp_path / "o.jsonl", deadline=14, clock=lambda: float(next(ticks)))

    kept = read_outcomes(tmp_path / "o.jsonl")
    assert result.stopped_early and 0 < len(kept) < 3
    assert all(o.complete for o in list(kept.values())[:-1])  # only the prompt the deadline interrupted may be incomplete


def test_a_file_scored_by_another_judge_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "o.jsonl"
    write_outcomes(path, [Outcome("p1", 0.1, 0.9, "someone-else")])
    before = path.read_bytes()
    fakes = Fakes(full_table())

    with pytest.raises(OutcomesError, match="not comparable"):
        run(fakes, path)

    assert path.read_bytes() == before and fakes.calls == []


def test_a_file_from_another_dataset_version_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "o.jsonl"
    write_outcomes(path, [Outcome("p1", 0.1, 0.9, JUDGE), Outcome("old-prompt", 0.1, 0.9, JUDGE)])
    before = path.read_bytes()

    with pytest.raises(OutcomesError, match="another dataset version"):
        run(Fakes(full_table()), path)

    assert path.read_bytes() == before


def test_the_sidecar_records_names_and_counts_and_no_text(tmp_path):
    result = run(Fakes(full_table()), tmp_path / "o.jsonl")
    meta = tmp_path / "o.meta.json"

    route_label.write_meta(meta, local=LOCAL, strong=STRONG, judge=JUDGE, dataset={"name": "route-domains", "version": "v1"}, run=result,
                           finished_at="2026-10-19T00:00:00Z")

    body = json.loads(meta.read_text())
    assert body["judge"] == JUDGE and body["rubric_sha256"] == route_label.RUBRIC_SHA256 and body["last_run"]["scored"] == 3
    assert "question number" not in meta.read_text()
