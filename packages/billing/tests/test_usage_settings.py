"""The credit settings (credit_billing/settings.py; moved from the agent app's config by specs/013): a price is a product decision, so none is
shipped, and a misconfiguration is refused at startup rather than silently pricing or gating wrongly.

`_env_file=None` and a scrubbed environment: `BillingSettings()` otherwise reads the developer's own `.env`, and a test of the DEFAULTS must not
depend on whether someone has set a rate."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from credit_billing.settings import BillingSettings as Settings

_CREDIT_ENV = (
    "CREDITS_PER_USD", "MARKUP", "CREDITS_ENFORCEMENT", "CREDIT_CHECK_FAILURE_POLICY",
    "BILLING_EXPORT_BATCH_SIZE", "BILLING_EXPORT_INTERVAL_SECONDS", "BILLING_EXPORT_MAX_ATTEMPTS", "BILLING_EXPORT_BACKOFF_BASE_SECONDS",
    "BILLING_EXPORT_BACKOFF_CAP_SECONDS", "BILLING_EXPORT_MAX_AGE_DAYS", "BILLING_EXPORT_CALL_TIMEOUT_SECONDS",
    "CREDIT_RECONCILE_SETTLE_SECONDS", "CREDIT_RECONCILE_LOOKBACK_DAYS", "CREDIT_RECONCILE_INTERVAL_SECONDS", "BILLING_USAGE_EVENT_RETENTION_DAYS",
)


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


class TestTheExportBounds:
    """Every loop in the export path has a ceiling, and the ceilings cannot be configured away."""

    def test_the_defaults_leave_the_age_limit_inside_stripes_35_day_window(self):
        settings = Settings(_env_file=None)

        assert settings.billing_export_max_age_days == 30 and settings.billing_export_max_age_days < 35

    @pytest.mark.parametrize("days", [35, 90])
    def test_an_age_limit_at_or_past_the_providers_window_is_rejected(self, days):
        """Retrying past the window is not a delay, it is a silent discard by the provider."""
        with pytest.raises(ValidationError):
            Settings(_env_file=None, billing_export_max_age_days=days)

    @pytest.mark.parametrize(
        "field",
        [
            "billing_export_batch_size", "billing_export_interval_seconds", "billing_export_max_attempts",
            "billing_export_backoff_base_seconds", "billing_export_backoff_cap_seconds",
            "billing_export_call_timeout_seconds", "billing_export_max_age_days",
        ],
    )
    def test_a_zero_ceiling_is_rejected_not_read_as_unbounded(self, field):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 0})

    def test_a_backoff_cap_below_its_base_is_rejected(self):
        with pytest.raises(ValidationError, match="CAP"):
            Settings(_env_file=None, billing_export_backoff_base_seconds=60, billing_export_backoff_cap_seconds=30)

    def test_a_batch_cannot_be_made_unboundedly_large(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, billing_export_batch_size=10**6)



class TestTheWalletReconciliationWindow:
    """The window the wallet reconciliation reads: the defaults an operator gets, and the bounds that stop a typo becoming a load on the database."""

    def test_the_defaults_are_a_quarter_hour_two_days_and_six_hours(self):
        settings = Settings(_env_file=None)

        assert (settings.credit_reconcile_settle_seconds, settings.credit_reconcile_lookback_days) == (900, 2)
        assert settings.credit_reconcile_interval_seconds == 6 * 3600

    @pytest.mark.parametrize(
        ("name", "value"),
        [("CREDIT_RECONCILE_SETTLE_SECONDS", "-1"), ("CREDIT_RECONCILE_LOOKBACK_DAYS", "0"), ("CREDIT_RECONCILE_LOOKBACK_DAYS", "36"), ("CREDIT_RECONCILE_INTERVAL_SECONDS", "59")],
    )
    def test_a_value_outside_its_bounds_refuses_to_start(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    @pytest.mark.parametrize(
        ("name", "value"),
        [("CREDIT_RECONCILE_SETTLE_SECONDS", "0"), ("CREDIT_RECONCILE_LOOKBACK_DAYS", "1"), ("CREDIT_RECONCILE_LOOKBACK_DAYS", "35"), ("CREDIT_RECONCILE_INTERVAL_SECONDS", "60")],
    )
    def test_the_bounds_themselves_are_allowed(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)

        Settings(_env_file=None)


class TestTheUsageEventRetention:
    """Below the floor a sweep would delete usage that a window still reads."""

    def test_the_default_keeps_a_year_and_then_some(self):
        assert Settings(_env_file=None).billing_usage_event_retention_days == 400

    def test_the_floor_covers_every_window_that_reads_the_events(self):
        """The wallet reconciliation reads back up to 35 days, the export outbox gives up at 30."""
        from credit_billing.settings import BILLING_USAGE_EVENT_MIN_RETENTION_DAYS

        assert BILLING_USAGE_EVENT_MIN_RETENTION_DAYS >= 35

    def test_the_floor_itself_is_accepted_and_less_refuses_to_start(self, monkeypatch):
        from credit_billing.settings import (
            BILLING_USAGE_EVENT_MIN_RETENTION_DAYS as FLOOR,
        )

        monkeypatch.setenv("BILLING_USAGE_EVENT_RETENTION_DAYS", str(FLOOR))
        assert Settings(_env_file=None).billing_usage_event_retention_days == FLOOR

        monkeypatch.setenv("BILLING_USAGE_EVENT_RETENTION_DAYS", str(FLOOR - 1))
        with pytest.raises(ValidationError, match="billing_usage_event_retention_days"):
            Settings(_env_file=None)
