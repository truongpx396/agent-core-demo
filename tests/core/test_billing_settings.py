"""The money-in settings (app/core/config.py): a webhook endpoint that cannot verify a signature must not start.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
import pytest
from pydantic import ValidationError

from app.core.config import BILLING_INBOX_MIN_RETENTION_DAYS, Settings

_ENV = (
    "BILLING_PROVIDERS", "BILLING_WEBHOOK_SECRETS", "BILLING_WEBHOOK_MAX_BODY_BYTES",
    "BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE", "BILLING_WEBHOOK_MAX_ATTEMPTS", "BILLING_REFUND_HOLD_HOURS",
    "BILLING_INBOX_RETENTION_DAYS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_by_default_no_provider_is_served():
    settings = Settings(_env_file=None)

    assert settings.billing_providers == "" and settings.billing_webhook_secrets == {}


def test_a_provider_with_a_secret_is_accepted(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake, stripe")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", '{"fake": "s1", "stripe": "s2"}')

    assert Settings(_env_file=None).billing_providers == "fake, stripe"


def test_an_enabled_provider_with_no_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake")

    with pytest.raises(ValidationError, match="no secret"):
        Settings(_env_file=None)


def test_one_enabled_provider_missing_its_secret_refuses_to_start_even_if_another_has_one(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake,stripe")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", '{"fake": "s1"}')

    with pytest.raises(ValidationError, match="stripe"):
        Settings(_env_file=None)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_secret_is_no_secret(monkeypatch, blank):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", f'{{"fake": "{blank}"}}')

    with pytest.raises(ValidationError, match="no secret"):
        Settings(_env_file=None)


def test_a_secret_never_shows_in_the_settings_repr(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDERS", "fake")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", '{"fake": "whsec_do_not_print_me"}')

    settings = Settings(_env_file=None)

    assert "whsec_do_not_print_me" not in repr(settings) and "whsec_do_not_print_me" not in str(settings)


@pytest.mark.parametrize("name", ["Stripe", "str/ipe", "a b", "../x"])
def test_a_provider_name_that_is_not_a_safe_path_segment_is_rejected(monkeypatch, name):
    monkeypatch.setenv("BILLING_PROVIDERS", name)
    monkeypatch.setenv("BILLING_WEBHOOK_SECRETS", f'{{"{name}": "s"}}')

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("billing_webhook_max_body_bytes", 10),
        ("billing_webhook_max_body_bytes", 10**9),
        ("billing_webhook_rate_limit_per_minute", 0),
        ("billing_webhook_max_attempts", 0),
        ("billing_refund_hold_hours", 0),
        ("billing_inbox_retention_days", BILLING_INBOX_MIN_RETENTION_DAYS - 1),
    ],
)
def test_a_bound_that_would_disable_a_guard_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
