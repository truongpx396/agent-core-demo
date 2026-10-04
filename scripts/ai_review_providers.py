"""Which providers the AI reviewer may try, in order, and how it moves from one to the next.

Everything about fallback providers lives here, so `scripts/ai_review.py` only has to hand it the one
thing it cannot know: how to ask a single provider. Stdlib only, no network, no GitHub types.
`walk_chain` takes that step (and the exception type that means "this provider failed") as
parameters, which is also what lets this module sit below `ai_review.py` without importing it.

The primary is `AI_REVIEW_BASE_URL` / `AI_REVIEW_MODEL` / `AI_REVIEW_API_KEY`. Up to `MAX_FALLBACKS`
fallbacks are numbered slots, tried in order when the one before has failed:

    AI_REVIEW_FALLBACK1_MODEL      required: a slot with no model is off
    AI_REVIEW_FALLBACK1_BASE_URL   optional: defaults to the primary's, i.e. "another model, same provider"
    AI_REVIEW_FALLBACK1_API_KEY    optional: see the key rule below
    AI_REVIEW_FALLBACK2_*          the same, for a third provider

Two-line reason this exists: a free tier's quota is per MODEL (and per provider), so when one is spent
the next may be untouched, and an advisory reviewer that goes dark for the day is worse than one that
quietly uses its second choice.

THE KEY RULE. One provider's API key is never sent to another provider's host. A fallback uses its own
key if it has one; otherwise it inherits the primary's key ONLY when it points at the same base URL
(the "another model, same provider" case). A fallback on a different host with no key of its own sends
none, which is right for a keyless endpoint and fails loudly with a 401 for one that needs a key.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

MAX_FALLBACKS = 2
DEADLINE_S = 480.0  # for the whole chain; the job's own limit is 600s (ai-review.yml)
MIN_ATTEMPT_S = 20.0  # not worth starting another provider with less than this left

T = TypeVar("T")
Http = Callable[[str, str, Mapping[str, str], bytes | None, float], Any]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class Provider:
    name: str  # "primary", "fallback 1", ...: for logs and the comment header
    base_url: str
    model: str
    api_key: str = ""


def _clean_url(value: str) -> str:
    return value.strip().rstrip("/")


def build_fallbacks(env: Mapping[str, str], primary: Provider) -> tuple[Provider, ...]:
    """The configured fallbacks, in order. A slot without a model is skipped; so is one that repeats a
    provider already in the chain (same base URL and model), since trying it again cannot help.

    Raises ValueError for a slot whose base URL is not http(s): the same check the primary gets (urllib
    would otherwise also open file:// and ftp:// URLs).
    """
    chain = [primary]
    for slot in range(1, MAX_FALLBACKS + 1):
        prefix = f"AI_REVIEW_FALLBACK{slot}_"
        model = (env.get(prefix + "MODEL") or "").strip()
        if not model:
            continue
        base_url = _clean_url(env.get(prefix + "BASE_URL") or "") or primary.base_url
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{prefix}BASE_URL must be an http(s) URL")
        key = (env.get(prefix + "API_KEY") or "").strip()
        if not key and base_url == primary.base_url:
            key = primary.api_key  # same host, so the same credential is the right one; see THE KEY RULE
        candidate = Provider(f"fallback {slot}", base_url, model, key)
        if any((p.base_url, p.model) == (candidate.base_url, candidate.model) for p in chain):
            continue
        chain.append(candidate)
    return tuple(chain[1:])


def describe_chain(chain: Sequence[Provider]) -> str:
    """The chain by name and model only (never a URL or a key), e.g. for a dry run."""
    return ", ".join(f"{provider.name} {provider.model}" for provider in chain)


def fallback_note(primary_model: str | None) -> str:
    """The comment header's note that a fallback answered; empty when the primary did."""
    return f" (fallback for `{primary_model}`, which was unavailable)" if primary_model else ""


def handover_notice(failed: Provider, reason: str, following: Provider) -> str:
    """The log line for moving on. Names and an error code only: never a URL, a key or message text."""
    return f"::notice::AI review: {failed.name} ({failed.model}) failed ({reason}); trying {following.name} ({following.model})"


def walk_chain(
    chain: Sequence[Provider],
    attempt: Callable[[Provider, Http, Sleep], T],
    *,
    http: Http,
    sleep: Sleep,
    clock: Callable[[], float],
    failure: type[Exception],
    notify: Callable[[str], None] = print,
) -> tuple[T, Provider]:
    """The result of the first provider that answers, and which one it was.

    `attempt(provider, http, sleep)` asks one provider (with its own retries) and raises `failure` if
    it has failed for good; the next provider is then tried. Any other exception is a bug and
    propagates: moving on to a second provider must not hide it.

    The whole chain shares one deadline of `DEADLINE_S`. The `http` and `sleep` handed to `attempt`
    are wrapped to enforce it: a request's timeout is clamped to the time left, and a retry wait that
    would overrun it raises `failure` instead of sleeping. So a slow first choice cannot push the step
    past the job's own limit and turn an advisory check red.

    With a single provider this is just `attempt` on the original `http` and `sleep`, so a deployment
    without fallbacks behaves, and fails with the same messages, as it did before they existed.
    """
    if len(chain) == 1:
        return attempt(chain[0], http, sleep), chain[0]
    started = clock()

    def left() -> float:
        return DEADLINE_S - (clock() - started)

    def bounded_http(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Any:
        return http(method, url, headers, body, max(1.0, min(timeout, left())))

    def bounded_sleep(seconds: float) -> None:
        if seconds > left():
            raise failure("out of time for this review")
        sleep(seconds)

    failures: list[str] = []
    for index, provider in enumerate(chain):
        if left() < MIN_ATTEMPT_S:
            failures.append(f"{provider.name} ({provider.model}): not tried, out of time")
            break
        try:
            return attempt(provider, bounded_http, bounded_sleep), provider
        except failure as exc:
            failures.append(f"{provider.name} ({provider.model}): {exc}")
            if index + 1 < len(chain):
                notify(handover_notice(provider, str(exc), chain[index + 1]))
    raise failure("every provider failed: " + "; ".join(failures))
