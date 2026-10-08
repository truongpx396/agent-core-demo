"""The Stripe and Polar credential settings (app/core/config.py, specs/010 T029).

These tests pin only what the settings themselves promise (the Stripe adapter reads one of them, the Polar adapter is not built): a real key is
secret-wrapped, the obvious pasting mistake is refused at startup, a blank line in `.env` means "not set", Polar defaults to
its sandbox, and, the one that mattered, a rejected value is never printed back in the startup error.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.billing import providers
from app.core.config import Settings

_ENV = (
    "STRIPE_API_KEY", "POLAR_ACCESS_TOKEN", "POLAR_ENVIRONMENT",
    "BILLING_PROVIDERS", "BILLING_WEBHOOK_SECRETS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_by_default_nothing_is_configured_and_polar_is_the_sandbox():
    settings = Settings(_env_file=None)

    assert settings.stripe_api_key is None and settings.polar_access_token is None
    assert settings.polar_environment == "sandbox"


@pytest.mark.parametrize("key", ["sk_test_abc123", "rk_test_abc123", "sk_live_abc123", "rk_live_abc123"])
def test_a_secret_or_restricted_key_is_accepted_and_never_shows_in_a_repr(monkeypatch, key):
    monkeypatch.setenv("STRIPE_API_KEY", key)

    settings = Settings(_env_file=None)

    assert settings.stripe_api_key.get_secret_value() == key
    assert key not in repr(settings) and key not in str(settings.stripe_api_key)


def test_a_publishable_key_is_refused_because_it_cannot_do_anything_on_a_server(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "pk_test_abc123")

    with pytest.raises(ValidationError, match="publishable key"):
        Settings(_env_file=None)


@pytest.mark.parametrize("wrong", ["pk_test_abc123", "polar_oat_pasted_into_the_wrong_variable", "whsec_abc123"])
def test_a_rejected_stripe_value_is_never_printed_back(monkeypatch, wrong):
    """Pydantic's default error prints `input_value='<the raw value>'`. A settings error is printed at import time, to a
    terminal or a container log, so a credential pasted into the wrong variable would be echoed whole."""
    monkeypatch.setenv("STRIPE_API_KEY", wrong)

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert wrong not in str(caught.value) and wrong not in repr(caught.value)


def test_a_malformed_webhook_secrets_value_does_not_print_the_secrets_inside_it(monkeypatch):
    """The same leak, which predates the provider credentials: the signing secrets live in one JSON variable."""
    monkeypatch.setenv("BILLING_PROVIDERS", "fake")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", '{"fake": {"nested": "whsec_THE_SECRET"}}')

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert "whsec_THE_SECRET" not in str(caught.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_line_in_dot_env_means_not_set_not_an_empty_key(monkeypatch, blank):
    monkeypatch.setenv("STRIPE_API_KEY", blank)
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", blank)

    settings = Settings(_env_file=None)

    assert settings.stripe_api_key is None and settings.polar_access_token is None


def test_a_polar_token_is_secret_wrapped(monkeypatch):
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", "polar_oat_abc123")

    settings = Settings(_env_file=None)

    assert settings.polar_access_token.get_secret_value() == "polar_oat_abc123"
    assert "polar_oat_abc123" not in repr(settings)


def test_polar_can_be_pointed_at_production_only_by_saying_so(monkeypatch):
    monkeypatch.setenv("POLAR_ENVIRONMENT", "production")

    assert Settings(_env_file=None).polar_environment == "production"


def test_an_unknown_polar_environment_is_refused(monkeypatch):
    monkeypatch.setenv("POLAR_ENVIRONMENT", "staging")

    with pytest.raises(ValidationError, match="polar_environment"):
        Settings(_env_file=None)


def test_the_warning_in_env_example_is_still_true_while_polar_has_no_adapter():
    """`.env.example` tells the operator that `polar` must not go in BILLING_PROVIDERS because it has no adapter and an
    unregistered name makes the API refuse to start. When the Polar adapter is registered this fails, which is the prompt
    to rewrite that comment (and the settings' comment in config.py) instead of leaving it to mislead. (Stripe's adapter
    landed first, so its warning is gone and `stripe` is the example the comment now gives of a name that works.)"""
    assert set(providers.FACTORIES) == {"fake", "stripe"}
    with pytest.raises(providers.UnknownProvider):
        providers.build_configured(("polar",), {"polar": "whsec_x"})
    assert providers.build_configured(("stripe",), {"stripe": "whsec_x"})["stripe"].name == "stripe"
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "POLAR adapter is NOT BUILT YET" in example and "STRIPE adapter is built" in example


def test_there_is_no_usage_event_name_setting_because_the_model_is_prepaid_packs():
    """Spec D16: nothing is sent to a provider as usage, so nothing needs a Stripe Meter or a Polar event name. A setting
    that nothing reads is a promise the code does not keep; it returns with the adapter that exports usage."""
    assert not {name for name in Settings.model_fields if "event_name" in name}
