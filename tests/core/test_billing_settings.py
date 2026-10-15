"""What is the app's about the billing settings: the app's `Settings` INHERITS the package's, and `.env.example` describes them truthfully.

The validators themselves (a provider with no secret refuses to start, a bound that would disable a guard, a credential's prefix, a blank means "not
set") are the package's and are tested there (`packages/billing/tests/test_webhook_settings.py`, `test_provider_credentials_settings.py`), against
`BillingSettings`. What is left to prove here is that none of it is lost on the way into the app's own `Settings`.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from credit_billing import providers
from credit_billing.settings import BillingSettings

_ENV = ("STRIPE_API_KEY", "POLAR_ACCESS_TOKEN", "POLAR_ENVIRONMENT", "BILLING_PROVIDERS", "BILLING_WEBHOOK_SECRETS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_apps_settings_carry_every_field_of_the_packages():
    assert issubclass(Settings, BillingSettings)
    assert set(BillingSettings.model_fields) <= set(Settings.model_fields)


def test_a_provider_with_no_secret_still_refuses_to_start_through_the_apps_settings(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake")

    with pytest.raises(ValidationError, match="no secret"):
        Settings(_env_file=None)


def test_a_rejected_credential_is_never_printed_back_by_the_apps_settings(monkeypatch):
    """`hide_input_in_errors` is set on the app's own class as well as the package's: a settings error is printed at import time, to a terminal or a
    container log, so a credential pasted into the wrong variable would be echoed whole."""
    monkeypatch.setenv("STRIPE_API_KEY", "pk_test_abc123")

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert "pk_test_abc123" not in str(caught.value) and "pk_test_abc123" not in repr(caught.value)


def test_the_comment_in_env_example_names_the_adapters_that_exist_and_an_unbuilt_name_still_refuses_to_start():
    """`.env.example` tells the operator which providers can go in BILLING_PROVIDERS. When an adapter is registered or removed
    this fails, which is the prompt to rewrite that comment (and the settings' comment in config.py) instead of leaving it to
    mislead. (Stripe's landed first, then Polar's; PayPal's is the one still unbuilt, so it is the example of a name that refuses.)"""
    assert set(providers.FACTORIES) == {"fake", "stripe", "polar"}
    with pytest.raises(providers.UnknownProvider):
        providers.build_configured(("paypal",), {"paypal": "whsec_x"})
    for name in ("stripe", "polar"):
        assert providers.build_configured((name,), {name: "whsec_x"})[name].name == name
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "STRIPE and POLAR adapters are built" in example and "PAYPAL" in example
