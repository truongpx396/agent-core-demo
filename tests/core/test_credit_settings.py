"""The credit settings (app/core/config.py): a price is a product decision, so none is shipped, and a
misconfiguration is refused at startup rather than silently pricing or gating wrongly.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`,
and a test of the DEFAULTS must not depend on whether someone has set a rate."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_CREDIT_ENV = ("CREDITS_PER_USD", "MARKUP", "CREDITS_ENFORCEMENT", "CREDIT_CHECK_FAILURE_POLICY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _CREDIT_ENV:
        monkeypatch.delenv(name, raising=False)


def test_no_price_is_shipped_so_no_accidental_one_goes_live():
    assert Settings(_env_file=None).credits_per_usd is None


def test_the_defaults_leave_credits_off_and_the_gate_off():
    settings = Settings(_env_file=None)

    assert settings.markup == Decimal("1"), "a neutral multiplier: pricing at cost, not a hidden margin"
    assert settings.credits_enforcement is False
    assert settings.credit_check_failure_policy == "open"


def test_a_rate_is_read_as_an_exact_decimal_not_a_float(monkeypatch):
    monkeypatch.setenv("CREDITS_PER_USD", "1000.5")
    monkeypatch.setenv("MARKUP", "1.15")

    settings = Settings(_env_file=None)

    assert settings.credits_per_usd == Decimal("1000.5") and isinstance(settings.credits_per_usd, Decimal)
    assert settings.markup == Decimal("1.15")


@pytest.mark.parametrize("field", ["credits_per_usd", "markup"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_rate_or_markup_that_is_not_above_zero_is_rejected(field, value):
    """0 would price every call at nothing; a negative would pay tenants to use the model."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", ["credits_per_usd", "markup"])
def test_more_than_six_decimal_places_is_rejected_because_the_event_row_could_not_store_it(field):
    with pytest.raises(ValidationError, match="6 decimal places"):
        Settings(_env_file=None, **{field: "1.0000001"})


def test_six_decimal_places_are_accepted():
    assert Settings(_env_file=None, credits_per_usd="0.000001").credits_per_usd == Decimal("0.000001")


def test_enforcement_without_a_rate_is_refused_at_startup():
    """A gate with no price cannot turn a hold in dollars into credits, so it must not start."""
    with pytest.raises(ValidationError, match="needs CREDITS_PER_USD"):
        Settings(_env_file=None, credits_enforcement=True)


def test_enforcement_with_a_rate_is_accepted():
    settings = Settings(_env_file=None, credits_enforcement=True, credits_per_usd="1000")

    assert settings.credits_enforcement is True


def test_a_rate_without_enforcement_is_shadow_mode_and_valid():
    settings = Settings(_env_file=None, credits_per_usd="1000")

    assert settings.credits_enforcement is False


def test_an_unknown_failure_policy_is_rejected_not_read_as_open():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, credit_check_failure_policy="maybe")
