"""Hermetic tests for `tests/deepeval/judge_fallback.py` — the deepeval
judges' backup chain: the hand-off rule (`call_with_fallbacks` /
`acall_with_fallbacks`) and which providers/models make up the chain
(`backup_chain_spec`). Deliberately NOT marked `deepeval`: they call no
model and need no key, so they run in the default `make test` tier on every
PR (the `deepeval`-marked files beside this one are manual/advisory, and a
rule that decides whether a rate-limited judge hands off or fails the run
shouldn't only be exercised by a run that has already hit the limit).

The chain exists because the original single `plugsky-micro` backup left a
gap: once the primary (Gemini/Groq) rate-limited AND `plugsky-micro` did
too, the second error propagated and failed the test even though
`plugsky-lite` was never tried. Nothing here proves a provider's real limits
(Plugsky's 30 req/min is per plan; OpenRouter's free 20 req/min + 50/day is
account-wide, not per model); it proves the hand-off rule and the ordering.
"""
import httpx
import openai
import pytest

from tests.deepeval.judge_fallback import (
    OPENROUTER,
    PLUGSKY,
    acall_with_fallbacks,
    backup_chain_spec,
    call_with_fallbacks,
    parse_model_list,
)


def _rate_limited() -> openai.RateLimitError:
    """The real exception class Groq's/Plugsky's OpenAI-SDK client raises —
    `_is_transient_provider_error` matches it by type, not by `.code`."""
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.plugsky.com/v1/chat/completions"))
    return openai.RateLimitError("rate limited", response=response, body=None)


class _CodedError(Exception):
    """Gemini's google-genai errors carry the HTTP status as `.code`
    instead of subclassing an openai type — the other branch of
    `_is_transient_provider_error`."""

    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


class _FakeModel:
    """Stands in for a deepeval model: records how often it was called and
    either returns `result` or raises `error`."""

    def __init__(self, name: str, result: str = "ok", error: Exception | None = None):
        self._name = name
        self._result = result
        self._error = error
        self.calls = 0

    def get_model_name(self) -> str:
        return self._name

    def run(self) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    async def arun(self) -> str:
        return self.run()


def _sync(models):
    return call_with_fallbacks(models, lambda model: model.run())


async def _async(models):
    return await acall_with_fallbacks(models, lambda model: model.arun())


def test_a_healthy_primary_never_touches_the_backups():
    primary, micro, lite = _FakeModel("gemini", "from-primary"), _FakeModel("plugsky-micro"), _FakeModel("plugsky-lite")

    assert _sync([primary, micro, lite]) == "from-primary"
    assert (micro.calls, lite.calls) == (0, 0)


def test_a_rate_limited_primary_falls_back_to_the_first_backup_only():
    primary = _FakeModel("gemini", error=_rate_limited())
    micro, lite = _FakeModel("plugsky-micro", "from-micro"), _FakeModel("plugsky-lite", "from-lite")

    assert _sync([primary, micro, lite]) == "from-micro"
    assert lite.calls == 0


def test_a_rate_limited_first_backup_hands_off_to_plugsky_lite():
    """The gap this chain closes: before it, `plugsky-micro`'s own 429 was
    the end of the road."""
    primary = _FakeModel("gemini", error=_CodedError(429))
    micro = _FakeModel("plugsky-micro", error=_rate_limited())
    lite = _FakeModel("plugsky-lite", "from-lite")

    assert _sync([primary, micro, lite]) == "from-lite"
    assert (primary.calls, micro.calls, lite.calls) == (1, 1, 1)


def test_an_exhausted_chain_raises_the_last_links_error_so_ci_can_rerun_the_test():
    first, second, last = _CodedError(503), _rate_limited(), _CodedError(429)
    models = [
        _FakeModel("gemini", error=first),
        _FakeModel("plugsky-micro", error=second),
        _FakeModel("plugsky-lite", error=last),
    ]

    with pytest.raises(_CodedError) as raised:
        _sync(models)

    assert raised.value is last


def test_a_non_transient_error_stops_the_chain_instead_of_being_masked():
    """A bad model name or revoked key (404/401) can't be fixed by trying
    the next model, so a later link must not get the chance to hide it."""
    primary = _FakeModel("gemini", error=_CodedError(429))
    micro = _FakeModel("plugsky-micro", error=_CodedError(401))
    lite = _FakeModel("plugsky-lite", "from-lite")

    with pytest.raises(_CodedError) as raised:
        _sync([primary, micro, lite])

    assert raised.value.code == 401
    assert lite.calls == 0


def test_a_non_transient_error_from_the_primary_never_reaches_any_backup():
    primary = _FakeModel("gemini", error=ValueError("bad schema"))
    micro = _FakeModel("plugsky-micro")

    with pytest.raises(ValueError, match="bad schema"):
        _sync([primary, micro])

    assert micro.calls == 0


def test_a_single_model_chain_just_calls_it_and_lets_any_error_through():
    only = _FakeModel("gemini", error=_CodedError(429))

    with pytest.raises(_CodedError):
        _sync([only])

    assert only.calls == 1


async def test_async_a_rate_limited_first_backup_hands_off_to_plugsky_lite():
    primary = _FakeModel("gemini", error=_CodedError(503))
    micro = _FakeModel("plugsky-micro", error=_rate_limited())
    lite = _FakeModel("plugsky-lite", "from-lite")

    assert await _async([primary, micro, lite]) == "from-lite"


async def test_async_an_exhausted_chain_raises_the_last_links_error():
    last = _CodedError(429)
    models = [_FakeModel("gemini", error=_CodedError(503)), _FakeModel("plugsky-micro", error=last)]

    with pytest.raises(_CodedError) as raised:
        await _async(models)

    assert raised.value is last


async def test_async_a_non_transient_error_is_not_swallowed():
    """The async twin has its own `try`, so it needs its own proof that
    `await` happens inside it — an un-awaited coroutine would return
    without ever raising and this would fail by getting a coroutine back."""
    primary = _FakeModel("gemini", error=ValueError("bad schema"))
    micro = _FakeModel("plugsky-micro", "from-micro")

    with pytest.raises(ValueError, match="bad schema"):
        await _async([primary, micro])

    assert micro.calls == 0


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", ",,"],
)
def test_a_blank_models_env_means_the_providers_default_not_no_backup(raw):
    assert parse_model_list(raw, ("a", "b")) == ("a", "b")


def test_a_models_env_is_an_ordered_list_and_a_single_name_still_works():
    assert parse_model_list("plugsky-lite , plugsky-micro", ("x",)) == ("plugsky-lite", "plugsky-micro")
    assert parse_model_list("plugsky-micro", ("x",)) == ("plugsky-micro",)


def _names(spec):
    return [(provider.name, model) for provider, model in spec]


def test_no_api_key_means_no_backup_chain_at_all():
    assert backup_chain_spec({}) == []
    # An empty secret (CI passes `${{ secrets.X }}` through even when unset) is "not set".
    assert backup_chain_spec({"PLUGSKY_API_KEY": "", "OPENROUTER_API_KEY": ""}) == []


def test_models_env_without_a_key_does_not_enable_a_provider():
    assert backup_chain_spec({"DEEPEVAL_BACKUP_MODEL": "plugsky-micro"}) == []


def test_the_default_chain_is_plugsky_micro_then_lite_then_openrouter_so_the_thinnest_quota_is_last():
    spec = backup_chain_spec({"PLUGSKY_API_KEY": "k1", "OPENROUTER_API_KEY": "k2"})

    assert _names(spec) == [
        ("plugsky", "plugsky-micro"),
        ("plugsky", "plugsky-lite"),
        ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
        ("openrouter", "qwen/qwen3.8-27b:free"),
    ]


def test_each_provider_is_enabled_by_its_own_key_alone():
    only_plugsky = backup_chain_spec({"PLUGSKY_API_KEY": "k"})
    only_openrouter = backup_chain_spec({"OPENROUTER_API_KEY": "k"})

    assert {provider for provider, _ in only_plugsky} == {PLUGSKY}
    assert {provider for provider, _ in only_openrouter} == {OPENROUTER}


def test_each_providers_models_env_overrides_only_that_provider():
    spec = backup_chain_spec(
        {
            "PLUGSKY_API_KEY": "k1",
            "OPENROUTER_API_KEY": "k2",
            "DEEPEVAL_OPENROUTER_MODEL": "qwen/qwen3.8-27b:free, google/gemma-4-31b-it:free",
        }
    )

    assert _names(spec) == [
        ("plugsky", "plugsky-micro"),
        ("plugsky", "plugsky-lite"),
        ("openrouter", "qwen/qwen3.8-27b:free"),
        ("openrouter", "google/gemma-4-31b-it:free"),
    ]


def test_the_original_single_backup_knob_still_selects_plugsky_models():
    """`DEEPEVAL_BACKUP_MODEL` predates the chain; an existing `.env` that
    sets one name must keep meaning "that Plugsky model", not be ignored."""
    spec = backup_chain_spec({"PLUGSKY_API_KEY": "k", "DEEPEVAL_BACKUP_MODEL": "plugsky-lite"})

    assert _names(spec) == [("plugsky", "plugsky-lite")]


def test_every_default_openrouter_model_is_a_free_variant():
    """A paid id here would bill the account on a fallback nobody is
    watching; `:free` is the contract this provider is picked on."""
    assert OPENROUTER.default_models
    assert all(model.endswith(":free") for model in OPENROUTER.default_models)
