"""`make router-label`: run the route prompts through the local and the strong model and have a judge score both answers (specs/014 C5, T071).

The output is the raw material for a routing head: for every prompt, how good each model's answer was. It is produced ON REQUEST and per
operator, because it costs minutes of model time per prompt and because the result is a fact about two specific models and one specific
judge (`routing.py` explains why that makes it a file the operator owns, not part of the committed dataset).

BOUNDED, because this runs unattended against a laptop's models: every call has its own timeout, and the whole run has a deadline after
which no new call starts. RESUMABLE: the outcomes file is rewritten atomically after every prompt, a re-run skips the prompts that already
have both scores, and retries the ones that were left missing. NEVER TENANT DATA: the only prompts it reads are the committed `route-domains`
set, which was written for this repository and checked against the tenant denylist when the set was frozen.

THE JUDGE sees the question and one answer and nothing else: not which model wrote it, not the other answer. A score it cannot produce
(not JSON, not a number in [0, 1], or out of range) is retried ONCE; if the retry fails too, the score is recorded as missing (`null`), never
as zero. A model call that times out or errors is missing too. Nothing here logs a prompt or an answer: counts and ids only.

KNOWN LIMITS (they bound what any router judged on this can be): the local and strong models answer WITHOUT retrieval, so a prompt about a
company policy is scored on how honestly the model handles not knowing it, not on a grounded answer; and a judge that is also the strong
model favours its own style. Both are disclosed in the PR that adds this, not hidden in a number.
"""
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.model_eval.routing import (
    Outcome,
    OutcomesError,
    read_outcomes,
    write_outcomes,
)

# (alias, messages, per-call timeout in seconds) -> the reply text. Injected so the tests use fakes and the CLI uses the gateway.
Chat = Callable[[str, list[dict[str, str]], float], str]

RUBRIC = (
    "You grade one answer from an AI assistant that helps a company's staff. Judge only whether the ANSWER is correct, complete and useful "
    "for the QUESTION, and whether it avoids inventing specifics it cannot know: facts about this particular company that the assistant was "
    "not given should be flagged as unknown, not made up. 1.0 = fully correct, complete and honest. 0.5 = partly right, vague, or "
    "missing something that matters. 0.0 = wrong, empty, evasive, or it invents facts. "
    'Reply with ONLY a JSON object of this exact form: {"score": <a number from 0 to 1>}. No other text.'
)
RUBRIC_SHA256 = hashlib.sha256(RUBRIC.encode()).hexdigest()
MAX_ANSWER_CHARS = 6000  # a runaway answer must not become an enormous judge prompt
_BARE_NUMBER = re.compile(r"^\s*(?:0(?:\.\d+)?|1(?:\.0+)?|\.\d+)\s*$")


@dataclass
class LabelRun:
    """What one run did, in counts only (no text): the CLI prints it and the sidecar file records it."""

    scored: int = 0  # prompts whose row was written complete in this run
    skipped: int = 0  # prompts that already had both scores
    missing: int = 0  # prompts written this run with at least one score missing
    stopped_early: bool = False  # the deadline passed before every prompt was attempted
    failures: dict[str, int] = field(default_factory=dict)  # "answer-error", "judge-malformed", ... -> count
    seconds: float = 0.0

    def count(self, kind: str) -> None:
        self.failures[kind] = self.failures.get(kind, 0) + 1


def parse_score(reply: str) -> float | None:
    """The judge's score, or None if the reply is not unambiguously one. Strict on purpose: "I'd say 0.8 out of 10" must not become 0.8, and a
    reply that merely CONTAINS a number somewhere is not a score."""
    text = reply.strip()
    if text.startswith("```"):  # a model that wraps its JSON in a code fence still answered the question
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    value: object
    try:
        parsed = json.loads(text)
    except ValueError:
        return float(text) if _BARE_NUMBER.match(text) else None
    value = parsed.get("score") if isinstance(parsed, dict) else parsed  # an object with a numeric `score` is unambiguous even beside a `reason`
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


def judge_messages(question: str, answer: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": RUBRIC},
        {"role": "user", "content": f"QUESTION:\n{question}\n\nANSWER:\n{answer[:MAX_ANSWER_CHARS]}"},
    ]


def _check_resume(existing: dict[str, Outcome], prompt_ids: set[str], judge: str, path: Path) -> None:
    unknown = sorted(set(existing) - prompt_ids)
    if unknown:
        raise OutcomesError(
            f"{path.name} has {len(unknown)} prompt id(s) that are not in this dataset (first: {unknown[0]!r}); it belongs to another "
            "dataset version. Use a different --strong-id or move the file aside; nothing was changed."
        )
    judges = {o.judge for o in existing.values()}
    if judges - {judge}:
        raise OutcomesError(
            f"{path.name} was scored by {sorted(judges)}, not {judge!r}. Scores from two judges are not comparable, so they are never mixed "
            "in one file; use a different --strong-id or move the file aside. Nothing was changed."
        )


def label_prompts(
    prompts: Sequence[dict],
    *,
    local: str,
    strong: str,
    judge: str,
    chat: Chat,
    out_path: Path,
    call_timeout: float = 180.0,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LabelRun:
    """Score every prompt (`{"id", "text"}`) with both models, writing `out_path` after each one. See the module docstring for the contract."""
    started = clock()
    run = LabelRun()
    existing = read_outcomes(out_path) if out_path.exists() else {}
    _check_resume(existing, {p["id"] for p in prompts}, judge, out_path)
    rows: dict[str, Outcome] = dict(existing)

    calls = 0  # model or judge calls made for the prompt in hand: a prompt the deadline reached before its first call was never attempted

    def over_time() -> bool:
        return deadline is not None and clock() - started >= deadline

    def score(question: str, alias: str) -> float | None:
        nonlocal calls
        if over_time():
            return None
        calls += 1
        try:
            answer = chat(alias, [{"role": "user", "content": question}], call_timeout)
        except Exception as exc:  # noqa: BLE001 - a timeout, a 5xx, a refused connection: this answer is missing, the run goes on
            run.count(f"answer-error:{type(exc).__name__}")
            return None
        for _attempt in range(2):  # one retry, then the score is missing
            if over_time():
                return None
            calls += 1
            try:
                reply = chat(judge, judge_messages(question, answer), call_timeout)
            except Exception as exc:  # noqa: BLE001 - same: the judge being down for one call must not stop the run
                run.count(f"judge-error:{type(exc).__name__}")
                continue
            value = parse_score(reply)
            if value is not None:
                return value
            run.count("judge-malformed")
        return None

    for prompt in prompts:
        if over_time():
            run.stopped_early = True
            break
        if prompt["id"] in rows and rows[prompt["id"]].complete:
            run.skipped += 1
            continue
        calls = 0
        outcome = Outcome(prompt["id"], score(prompt["text"], local), score(prompt["text"], strong), judge)
        if calls == 0:  # the deadline arrived between the check above and the first call: leave no row, so a resume sees it as untouched
            run.stopped_early = True
            break
        rows[prompt["id"]] = outcome
        run.scored += outcome.complete
        run.missing += not outcome.complete
        write_outcomes(out_path, [rows[p["id"]] for p in prompts if p["id"] in rows])
        if over_time() and not outcome.complete:
            run.stopped_early = True
            break
    run.seconds = clock() - started
    return run


def gateway_chat() -> Chat:
    """The LiteLLM gateway the app itself uses (OPENAI_API_BASE), temperature 0 so a re-run asks the same question the same way."""
    from openai import OpenAI

    from app.core.config import OPENAI_API_BASE, OPENAI_API_KEY

    client = OpenAI(base_url=OPENAI_API_BASE, api_key=OPENAI_API_KEY, max_retries=0)  # no hidden retries: the timeout below is the bound

    def chat(alias: str, messages: list[dict[str, str]], timeout: float) -> str:
        reply = client.chat.completions.create(model=alias, messages=messages, temperature=0, timeout=timeout)  # type: ignore[arg-type]  # plain dicts are valid messages
        return reply.choices[0].message.content or ""

    return chat


def write_meta(path: Path, *, local: str, strong: str, judge: str, dataset: dict, run: LabelRun, finished_at: str) -> None:
    """A sidecar next to the outcomes (counts and names only): which models and judge the scores are a fact about."""
    path.write_text(
        json.dumps(
            {"local": local, "strong": strong, "judge": judge, "rubric_sha256": RUBRIC_SHA256, "dataset": dataset, "finished_at": finished_at,
             "last_run": {"scored": run.scored, "skipped": run.skipped, "missing": run.missing, "stopped_early": run.stopped_early,
                          "failures": run.failures, "seconds": round(run.seconds, 1)}},
            indent=2, sort_keys=True,
        ) + "\n"
    )
