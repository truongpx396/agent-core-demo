"""Which providers the AI reviewer may try, in order, and how it moves from one to the next.

Everything about fallback providers lives here, so `scripts/ai_review.py` only has to hand it the one
thing it cannot know: how to ask a single provider. Stdlib only, no network, no GitHub types.
`walk_chain` takes that step (and the exception type that means "this provider failed") as
parameters, which is also what lets this module sit below `ai_review.py` without importing it.

The primary is `AI_REVIEW_BASE_URL` / `AI_REVIEW_MODEL` / `AI_REVIEW_API_KEY`. The fallbacks are grouped
by provider, because that is the only distinction that matters (a model is just a name; a provider
brings a host and a key), and are tried in this order:

    AI_REVIEW_FALLBACK_MODELS              comma list: other models on the PRIMARY's provider (same host, same key)
    AI_REVIEW_FALLBACK_PROVIDER1_MODELS    comma list: models on ANOTHER provider; empty switches the group off
    AI_REVIEW_FALLBACK_PROVIDER1_BASE_URL  that provider's host (unset = the primary's, i.e. another account)
    AI_REVIEW_FALLBACK_PROVIDER1_API_KEY   that provider's key: see the key rule below
    AI_REVIEW_FALLBACK_PROVIDER2_*         the same, for a third provider

The chain is the primary, then FALLBACK_MODELS in order, then PROVIDER1's models, then PROVIDER2's. In logs
and in the comment header each is "fallback N" by its place in that chain.

Two-line reason this exists: a free tier's quota is per MODEL (and per provider), so when one is spent
the next may be untouched, and an advisory reviewer that goes dark for the day is worse than one that
quietly uses its second choice.

THE KEY RULE. One provider's API key is never sent to another provider's host. A fallback uses its own
key if it has one; otherwise it inherits the primary's key ONLY when it points at the same base URL
(the FALLBACK_MODELS case). A fallback on a different host with no key of its own sends
none, which is right for a keyless endpoint and fails loudly with a 401 for one that needs a key.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

MAX_PROVIDERS = 2  # the numbered provider groups, besides the primary's own FALLBACK_MODELS
MAX_FALLBACKS = 6  # in all. The diff goes to every one tried, so a typo'd list must not fan out
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


def model_list(value: str | None) -> list[str]:
    """A comma-separated list of model names: stripped, blanks and repeats dropped, order kept."""
    names: list[str] = []
    for part in (value or "").split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def build_fallbacks(env: Mapping[str, str], primary: Provider) -> tuple[Provider, ...]:
    """The configured fallbacks, in the order they are tried (see the module docstring).

    A group without models is off, whatever else it sets. A model that repeats one already in the chain
    (same base URL and model) is dropped, since trying it again cannot help.

    Raises ValueError, naming the variable, for a base URL that is not http(s) (the check the primary
    gets; urllib would otherwise also open file:// and ftp:// URLs) and for more than MAX_FALLBACKS
    models in all: the diff goes to each one tried, so a mistake there should be loud, not silent.
    """
    chain = [primary]

    def add(models: list[str], base_url: str, key: str) -> None:
        for model in models:
            candidate = Provider(f"fallback {len(chain)}", base_url, model, key)
            if not any((p.base_url, p.model) == (candidate.base_url, candidate.model) for p in chain):
                chain.append(candidate)

    # The primary's own provider: same host, so the same credential is the right one (see THE KEY RULE).
    add(model_list(env.get("AI_REVIEW_FALLBACK_MODELS")), primary.base_url, primary.api_key)
    for number in range(1, MAX_PROVIDERS + 1):
        prefix = f"AI_REVIEW_FALLBACK_PROVIDER{number}_"
        models = model_list(env.get(prefix + "MODELS"))
        if not models:
            continue
        base_url = _clean_url(env.get(prefix + "BASE_URL") or "") or primary.base_url
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{prefix}BASE_URL must be an http(s) URL")
        key = (env.get(prefix + "API_KEY") or "").strip()
        if not key and base_url == primary.base_url:
            key = primary.api_key
        add(models, base_url, key)
    if len(chain) - 1 > MAX_FALLBACKS:
        raise ValueError(f"AI_REVIEW_FALLBACK_* name {len(chain) - 1} fallback models; at most {MAX_FALLBACKS} are used")
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
