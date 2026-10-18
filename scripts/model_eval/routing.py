"""Routing outcomes: what the local and the strong model scored on each route prompt, and the label a head learns from them (specs/014 C5).

An outcome row is `{"id", "local", "strong", "judge"}`: the judge's 0-1 score of each model's answer to the prompt `id`, and the alias of the
judge that gave them. A score may be `null`: the call timed out, the model errored, or the judge twice failed to produce a score. That is
RECORDED AS MISSING and never as zero, because zero says "the answer was worthless" and a missing score says "we do not know"; a head trained
on invented zeros learns to route the prompts a flaky model failed on, not the prompts it is bad at.

A label is a fact about two specific models and one specific judge (a prompt that needs the 7B model with one judge may not with another), so
outcomes are produced per operator into an ignored directory and are never part of a dataset's manifest hash. Everything a head records about
where its labels came from is therefore the sha256 of the exact file it was trained on.
"""
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MARGIN = 0.2  # strong - local must reach this for a prompt to count as "needs the stronger model"
_LABEL_PLACES = 9  # see label(): compare gaps at nine decimals so 0.7 - 0.5 is the 0.2 it was written as


class OutcomesError(ValueError):
    """An outcomes file that cannot be trusted: malformed row, duplicate id, score outside [0, 1]."""


@dataclass(frozen=True)
class Outcome:
    id: str
    local: float | None
    strong: float | None
    judge: str

    @property
    def complete(self) -> bool:
        return self.local is not None and self.strong is not None

    def as_row(self) -> dict:
        return {"id": self.id, "local": self.local, "strong": self.strong, "judge": self.judge}


def outcomes_dir(dataset_dir: Path) -> Path:
    return dataset_dir / "outcomes"


def _score(value: object, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise OutcomesError(f"{where}: a score must be null or a number in [0, 1], got {value!r}")
    return float(value)


def parse_row(row: object, where: str) -> Outcome:
    if not isinstance(row, dict):
        raise OutcomesError(f"{where}: a row must be a JSON object")
    ident, judge = row.get("id"), row.get("judge")
    if not isinstance(ident, str) or not ident:
        raise OutcomesError(f"{where}: `id` must be a non-empty string")
    if not isinstance(judge, str) or not judge:
        raise OutcomesError(f"{where}: `judge` must be the non-empty alias of the judge that scored it")
    return Outcome(ident, _score(row.get("local"), f"{where} local"), _score(row.get("strong"), f"{where} strong"), judge)


def read_outcomes(path: Path) -> dict[str, Outcome]:
    """Every row of an outcomes file by prompt id. A malformed row or a repeated id is an error, not something to skip: a head must not be
    trained on a file nobody could vouch for."""
    out: dict[str, Outcome] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name} line {number}"
        try:
            outcome = parse_row(json.loads(line), where)
        except json.JSONDecodeError as exc:
            raise OutcomesError(f"{where}: not JSON ({exc.msg})") from exc
        if outcome.id in out:
            raise OutcomesError(f"{where}: prompt {outcome.id!r} appears twice")
        out[outcome.id] = outcome
    return out


def write_outcomes(path: Path, outcomes: Iterable[Outcome]) -> str:
    """Atomically replace `path` with these rows, in the order given (so a killed run leaves the previous complete file, never a torn one).
    Returns the sha256 of what was written."""
    body = "".join(json.dumps(o.as_row(), sort_keys=True) + "\n" for o in outcomes).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return hashlib.sha256(body).hexdigest()


def outcomes_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def label(outcome: Outcome, margin: float = DEFAULT_MARGIN) -> int:
    """1 (`needs_stronger`) iff the strong model scored at least `margin` above the local one on this prompt, else 0 (`local_ok`).

    The gap is rounded before the comparison because `0.7 - 0.5` is `0.19999999999999996` in floating point: without it a prompt whose gap is
    exactly the margin the operator asked for would be labelled `local_ok` depending on which two decimals happened to be involved."""
    if not outcome.complete:
        raise OutcomesError(f"prompt {outcome.id!r} has a missing score and cannot be labelled")
    assert outcome.local is not None and outcome.strong is not None  # narrowed by `complete`
    return int(round(outcome.strong - outcome.local, _LABEL_PLACES) >= round(margin, _LABEL_PLACES))
