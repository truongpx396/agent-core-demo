"""Hermetic tests for tests/deepeval/backup_providers.py: which free providers end a judge's failover chain.

Deliberately NOT marked `deepeval` (no key, no model, no network), so they run in the default suite. Nothing
here proves a provider's real limits (Plugsky's 30 req/min is per plan; OpenRouter's free 20 req/min + 50/day
is account-wide); it proves the ORDER, the key gating and the overrides, and that the chain really does walk
Plugsky micro -> lite -> OpenRouter on the failure that motivated it, using real exception shapes.

The hand-off rule itself is tested once, in test_fallback_judge.py; this file does not repeat it.
"""
import sys

import pytest

from tests.deepeval import fallback as f
from tests.deepeval.backup_providers import (
    BACKUP_PROVIDERS,
    OPENROUTER,
    PLUGSKY,
    backup_chain_spec,
    build_backup_models,
)
from tests.deepeval.test_fallback_judge import FakeModel, rate_limit, retry_error


def _names(spec):
    return [(provider.name, model) for provider, model in spec]


def test_no_api_key_means_no_backup_chain_at_all():
    assert backup_chain_spec({}) == []
    # CI passes `${{ secrets.X }}` through even when the secret is unset: an empty (or blank) key is "not set".
    assert backup_chain_spec({"PLUGSKY_API_KEY": "", "OPENROUTER_API_KEY": ""}) == []
    assert backup_chain_spec({"PLUGSKY_API_KEY": "  ", "OPENROUTER_API_KEY": "\n"}) == []


def test_a_models_variable_without_a_key_does_not_enable_a_provider():
    assert backup_chain_spec({"DEEPEVAL_BACKUP_MODEL": "plugsky-micro"}) == []
    assert backup_chain_spec({"DEEPEVAL_OPENROUTER_MODEL": "qwen/qwen3.8-27b:free"}) == []


def test_the_default_chain_is_plugsky_micro_then_lite_then_openrouter_so_the_thinnest_quota_is_last():
    spec = backup_chain_spec({"PLUGSKY_API_KEY": "k1", "OPENROUTER_API_KEY": "k2"})

    assert _names(spec) == [
        ("plugsky", "plugsky-micro"),
        ("plugsky", "plugsky-lite"),
        ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
        ("openrouter", "qwen/qwen3.8-27b:free"),
    ]
    assert BACKUP_PROVIDERS == (PLUGSKY, OPENROUTER)


def test_each_provider_is_enabled_by_its_own_key_alone():
    assert {provider for provider, _ in backup_chain_spec({"PLUGSKY_API_KEY": "k"})} == {PLUGSKY}
    assert {provider for provider, _ in backup_chain_spec({"OPENROUTER_API_KEY": "k"})} == {OPENROUTER}


def test_each_providers_models_variable_overrides_only_that_provider_and_keeps_its_order():
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
    """`DEEPEVAL_BACKUP_MODEL` predates the chain; an existing `.env` that names one model must keep meaning
    "that Plugsky model", not be ignored."""
    assert _names(backup_chain_spec({"PLUGSKY_API_KEY": "k", "DEEPEVAL_BACKUP_MODEL": "plugsky-lite"})) == [
        ("plugsky", "plugsky-lite")
    ]


@pytest.mark.parametrize("blank", ["", "   ", ",,"])
def test_a_blank_models_variable_means_the_providers_default_not_no_backup(blank):
    spec = backup_chain_spec({"PLUGSKY_API_KEY": "k", "DEEPEVAL_BACKUP_MODEL": blank})
    assert [model for _, model in spec] == list(PLUGSKY.default_models)


def test_the_word_none_switches_one_providers_models_off_even_with_its_key_set():
    spec = backup_chain_spec({"PLUGSKY_API_KEY": "k1", "OPENROUTER_API_KEY": "k2", "DEEPEVAL_BACKUP_MODEL": "none"})
    assert {provider for provider, _ in spec} == {OPENROUTER}


def test_every_default_openrouter_model_is_a_free_variant():
    """A paid id here would bill the account on a fallback nobody is watching; `:free` is the contract this provider is picked on."""
    assert OPENROUTER.default_models
    assert all(model.endswith(":free") for model in OPENROUTER.default_models)


def test_provider_names_and_hosts_are_distinct_so_a_key_can_never_be_sent_to_the_other_provider():
    assert PLUGSKY.base_url != OPENROUTER.base_url and PLUGSKY.api_key_env != OPENROUTER.api_key_env
    assert PLUGSKY.base_url.startswith("https://") and OPENROUTER.base_url.startswith("https://")


# --- building the models ---------------------------------------------------------------------


def test_with_no_key_it_builds_nothing_and_does_not_even_import_deepeval(monkeypatch):
    # The fast `test` job has no deepeval at all; "no backup configured" must not need it.
    monkeypatch.setitem(sys.modules, "deepeval", None)  # makes `import deepeval...` raise ImportError
    monkeypatch.setitem(sys.modules, "deepeval.models", None)
    assert build_backup_models({}) == []


def test_each_model_is_a_real_local_model_pointed_at_its_own_provider_with_its_own_key():
    pytest.importorskip("deepeval")

    built = build_backup_models({"PLUGSKY_API_KEY": " key-p ", "OPENROUTER_API_KEY": "key-o"})

    assert [m.get_model_name() for m in built] == [
        "plugsky-micro (Local Model)",
        "plugsky-lite (Local Model)",
        "nvidia/nemotron-3-super-120b-a12b:free (Local Model)",
        "qwen/qwen3.8-27b:free (Local Model)",
    ]
    hosts = [m.base_url for m in built]
    assert hosts == [PLUGSKY.base_url] * 2 + [OPENROUTER.base_url] * 2
    keys = [getattr(k, "get_secret_value", lambda k=k: k)() for k in (m.local_model_api_key for m in built)]  # SecretStr
    assert keys == ["key-p"] * 2 + ["key-o"] * 2  # each key only ever goes to its own host (and is stripped)


# --- the whole chain, with the real failure shape ----------------------------------------------


def test_a_rate_limit_walks_the_primary_then_plugsky_micro_then_lite_then_openrouter():
    """The case #108/#110 were written for, on the engine that can actually see it: every link fails with
    deepeval's real `RetryError`-wrapped 429 until the last OpenRouter model answers."""
    models = [
        FakeModel("primary", retry_error(rate_limit())),
        FakeModel("plugsky-micro", retry_error(rate_limit())),
        FakeModel("plugsky-lite", retry_error(rate_limit())),
        FakeModel("or-nemotron", retry_error(rate_limit())),
        FakeModel("or-qwen"),
    ]
    chain = f.build_chain(models[0], [], models[1:], notify=lambda _: None)

    assert chain.generate("grade this") == "answer-from-or-qwen"
    assert [len(m.calls) for m in models] == [1, 1, 1, 1, 1]  # each tried once, in order


def test_a_healthy_primary_never_touches_a_backup():
    models = [FakeModel("primary"), FakeModel("plugsky-micro"), FakeModel("or-qwen")]
    assert f.build_chain(models[0], [], models[1:]).generate("x") == "answer-from-primary"
    assert [len(m.calls) for m in models] == [1, 0, 0]


def test_a_retired_openrouter_model_fails_loudly_instead_of_being_skipped():
    """A `:free` id that has been retired returns a 404. That is not transient, and a fallback that can't fix
    it must not paper over it: it surfaces."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    gone = openai.NotFoundError("No endpoints found", response=httpx.Response(404, request=request), body=None)
    models = [FakeModel("primary", retry_error(rate_limit())), FakeModel("or-nemotron", gone), FakeModel("or-qwen")]

    with pytest.raises(openai.NotFoundError):
        f.build_chain(models[0], [], models[1:], notify=lambda _: None).generate("x")
    assert len(models[2].calls) == 0
