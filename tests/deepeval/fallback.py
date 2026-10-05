"""Failover between judge models for tests/deepeval. Test-support code; no `deepeval` import at module
level, because the fast `test` job collects tests/deepeval/ without `deepeval` installed at all.

THE BUG THIS REPLACES. The first fallback (a single optional Plugsky backup in conftest.py) only engaged
when `exc` was itself an `openai.RateLimitError` or carried an HTTP `.code`. But deepeval wraps every call in
a tenacity retry policy with `reraise=False` (deepeval 4.2.0 `models/retry_policy.py`), so when its attempts
run out the exception that reaches the test is `tenacity.RetryError`, with the real `RateLimitError` buried
inside it. A real CI run (PR #101) hit a Groq per-day token limit, had `PLUGSKY_API_KEY` set, and the backup
NEVER engaged: zero "falling back" lines, and `FAILED ... tenacity.RetryError: RetryError[<Future ... raised
RateLimitError>]`. The one case the fallback existed for was the one it could not see. `chain_of` now walks
into a RetryError and down `__cause__`/`__context__`, and the classifier looks at every exception in it.

THE CHAIN. Each judge fixture is an ordered list: its primary, then other models on the SAME provider (each
model has its own free-tier quota: Groq's 429 names the model, Gemini's names `PerProjectPerModel`), then
the optional Plugsky backup. A transient failure (rate limit, overload, timeout) moves to the next model;
so does an UNUSABLE ANSWER (below). Anything else (auth, a 400 bad schema, a real bug) is re-raised
untouched, because a different model cannot fix it and a fallback must not hide it.

UNUSABLE ANSWERS. The second gap, found in CI run 37320398499 (the merge of #121, and #120 before it): both
`test_conversation_simulator_deepeval.py` tests went red on `main` with the judge having ANSWERED, just not in
the shape asked. deepeval 4.2.0's `LocalModel` sends no `response_format` (so Groq is only asked for JSON in
the prompt), then runs `trim_and_load_json` and `schema.model_validate` on the text. One run got
`{"verdicts": {...}}` where a list was wanted (a pydantic `ValidationError`), the other an EMPTY completion
(`DeepEvalError: ... invalid JSON`, raised from inside `except JSONDecodeError`, so the `JSONDecodeError` is
only its `__context__`). Neither is transient and neither is a bug in the repo, so the chain re-raised them and
`flaky=True` (which only swallows a failed SCORE) could not help: the job went red on a judge's formatting. The
log shows the Groq primary had just run out of daily tokens, so the answers most likely came from a weaker
fallback; that is not proven, because a non-transient failure never named its model. Now it does. An unusable
answer moves to the next model for THAT call only: no cooldown, since the model is up and the next prompt may
suit it. If the last model also answers badly the error is raised, so a chain where nobody can answer still
fails the job rather than passing on nothing.

COOLDOWN. deepeval retries a dead model with backoff on EVERY call before the error surfaces, so a judge
whose daily quota is gone would cost that wait again on each of dozens of metric calls. A model that failed
transiently is skipped for a while (an hour if the error says it is a per-day limit, five minutes otherwise)
and tried again afterwards, since a rolling window recovers.

Logs name the model and the error CLASS and status only, never the provider's message: this repo's CI logs
are public and a 429 message carries the organisation id and quota figures.
"""
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# By class name (or any base class's name) so this module needs neither openai nor google-genai.
TRANSIENT_NAMES = frozenset(
    {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError", "ServerError", "TimeoutError"}
)
TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})
# A judge answer that arrived but cannot be used: pydantic rejecting its shape (`ValidationError`), or text that is
# not JSON (`JSONDecodeError`, which deepeval wraps in a DeepEvalError and leaves only as `__context__`). By name,
# for the same reason as above: this module imports neither pydantic nor deepeval.
UNUSABLE_NAMES = frozenset({"ValidationError", "JSONDecodeError"})
SHORT_COOLDOWN_S = 300.0
DAILY_COOLDOWN_S = 3600.0
_MAX_CHAIN = 8  # bound on how many wrapped exceptions are looked at: they can in principle form a cycle


def chain_of(exc: BaseException) -> list[BaseException]:
    """`exc` and everything wrapped inside it: a tenacity RetryError's last attempt, then `__cause__`
    and `__context__`, breadth first, each once."""
    found: list[BaseException] = []
    queue: list[BaseException | None] = [exc]
    while queue and len(found) < _MAX_CHAIN:
        current = queue.pop(0)
        if current is None or any(current is seen for seen in found):
            continue
        found.append(current)
        attempt = getattr(current, "last_attempt", None)  # tenacity.RetryError: the real error is in here
        if attempt is not None and callable(getattr(attempt, "done", None)) and attempt.done() and not attempt.cancelled():
            queue.append(attempt.exception())
        queue.extend([current.__cause__, current.__context__])
    return found


def _status(exc: BaseException) -> int | None:
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _named(exc: BaseException, names: frozenset[str]) -> bool:
    """True if `exc`'s class, or any base class, is called one of `names`."""
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def _is_transient_one(exc: BaseException) -> bool:
    return _named(exc, TRANSIENT_NAMES) or _status(exc) in TRANSIENT_CODES


def is_transient(exc: BaseException) -> bool:
    """True if any exception inside `exc` is a rate limit, an overload or a timeout."""
    return any(_is_transient_one(inner) for inner in chain_of(exc))


def is_unusable_output(exc: BaseException) -> bool:
    """True if any exception inside `exc` says the judge answered in a form deepeval could not use: invalid
    JSON, or JSON of the wrong shape. A different model may answer that same prompt properly."""
    return any(_named(inner, UNUSABLE_NAMES) for inner in chain_of(exc))


def describe(exc: BaseException) -> str:
    """For a log line: the class and status of the first transient or unusable-answer exception inside, never
    its message (a pydantic `ValidationError` quotes the model's answer, which can echo the conversation)."""
    for inner in chain_of(exc):
        if _is_transient_one(inner):
            status = _status(inner)
            return type(inner).__name__ + (f" {status}" if status is not None else "")
        if _named(inner, UNUSABLE_NAMES):
            return type(inner).__name__
    return type(exc).__name__


def cooldown_for(exc: BaseException) -> float:
    """How long to skip a model after this failure. A per-day limit will not clear in a CI run; anything
    else (a per-minute limit, an overload) is worth trying again soon."""
    text = " ".join(str(inner) for inner in chain_of(exc)).lower()
    daily = any(word in text for word in ("per day", "(tpd)", "(rpd)", "perday", "daily"))
    return DAILY_COOLDOWN_S if daily else SHORT_COOLDOWN_S


def model_list(value: str | None, default: Sequence[str], exclude: Sequence[str] = ()) -> list[str]:
    """Model names from a comma-separated variable: `default` when unset, blank or holding no name at all (a
    stray `,,` must not silently switch a provider off), none at all for the word `none`; blanks, repeats and
    anything in `exclude` (the primary) are dropped, order kept."""
    if value is not None and value.strip().lower() == "none":
        names: list[str] = []
    else:
        names = [name for name in (value or "").split(",") if name.strip()] or list(default)
    unique: list[str] = []
    for raw in names:
        name = raw.strip()
        if name and name not in unique and name not in exclude:
            unique.append(name)
    return unique


def _content(result: Any) -> Any:
    """deepeval's native models return `(content, cost)`; a custom `DeepEvalBaseLLM` must return the content
    alone, and callers branch on that (see `make_judge`)."""
    return result[0] if isinstance(result, tuple) else result


@dataclass(frozen=True)
class Link:
    name: str
    model: Any  # has generate/a_generate/get_model_name, like a deepeval model


class FailoverChain:
    """The first model that answers, in order, skipping any that failed recently."""

    def __init__(
        self,
        links: Sequence[Link],
        *,
        clock: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = print,
    ) -> None:
        if not links:
            raise ValueError("a failover chain needs at least one model")
        self._links = list(links)
        self._clock = clock
        self._notify = notify
        self._skip_until: dict[int, float] = {}

    @property
    def primary(self) -> Any:
        return self._links[0].model

    def _order(self) -> list[int]:
        now = self._clock()
        ready = [i for i in range(len(self._links)) if self._skip_until.get(i, 0.0) <= now]
        return ready or list(range(len(self._links)))  # every model is cooling: try them all rather than none

    def _failed(self, index: int, exc: BaseException, following: Link | None) -> None:
        if is_transient(exc):
            self._skip_until[index] = self._clock() + cooldown_for(exc)
            what = f"hit a transient error ({describe(exc)})"
        else:  # an unusable answer: the model is up, so no cooldown; the next prompt may suit it
            what = f"returned an unusable answer ({describe(exc)})"
        tail = f"; falling back to {following.name}" if following else "; no model left to fall back to"
        self._notify(f"[deepeval] {self._links[index].name} {what}{tail}")

    def generate(self, prompt: str, schema: Any = None) -> Any:
        order = self._order()
        for position, index in enumerate(order):
            try:
                return _content(self._links[index].model.generate(prompt, schema=schema))
            except Exception as exc:  # noqa: BLE001 - classified right below: anything that is neither transient nor an unusable answer is re-raised untouched
                if not (is_transient(exc) or is_unusable_output(exc)):
                    raise
                following = self._links[order[position + 1]] if position + 1 < len(order) else None
                self._failed(index, exc, following)
                if following is None:
                    raise
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    async def a_generate(self, prompt: str, schema: Any = None) -> Any:
        order = self._order()
        for position, index in enumerate(order):
            try:
                return _content(await self._links[index].model.a_generate(prompt, schema=schema))
            except Exception as exc:  # noqa: BLE001 - classified right below: anything that is neither transient nor an unusable answer is re-raised untouched
                if not (is_transient(exc) or is_unusable_output(exc)):
                    raise
                following = self._links[order[position + 1]] if position + 1 < len(order) else None
                self._failed(index, exc, following)
                if following is None:
                    raise
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def build_chain(primary: Any, fallbacks: Sequence[Any] = (), backups: Sequence[Any] = (), **kwargs: Any) -> FailoverChain:
    """primary, then the same-provider fallbacks in order, then the backup providers' models in order
    (tests/deepeval/backup_providers.py); each link is named by its model."""
    return FailoverChain([Link(model.get_model_name(), model) for model in (primary, *fallbacks, *backups)], **kwargs)


def make_judge(chain: FailoverChain) -> Any:
    """A `DeepEvalBaseLLM` that answers through `chain`.

    It MUST subclass DeepEvalBaseLLM, not just duck-type the methods: deepeval's `metrics/utils.py::
    initialize_model` does `isinstance(model, DeepEvalBaseLLM)` before trusting a passed-in model, and a plain
    wrapper silently falls through to env-based auto-detection, grading with the wrong model. And
    `generate`/`a_generate` return the CONTENT only, not the `(content, cost)` tuple the native models return:
    a custom subclass is flagged `using_native_model = False`, so callers take the return value as the content
    itself (a real crash in PR #44 before this was handled). `deepeval` is imported here, not at module level.
    """
    from deepeval.models import DeepEvalBaseLLM

    class _FailoverJudge(DeepEvalBaseLLM):
        def __init__(self, failover: FailoverChain) -> None:
            self._chain = failover
            super().__init__(failover.primary.get_model_name())

        def load_model(self) -> Any:
            return self._chain.primary

        def get_model_name(self) -> str:
            return str(self._chain.primary.get_model_name())

        def generate(self, prompt: str, schema: Any = None) -> Any:
            return self._chain.generate(prompt, schema=schema)

        async def a_generate(self, prompt: str, schema: Any = None) -> Any:
            return await self._chain.a_generate(prompt, schema=schema)

    return _FailoverJudge(chain)
