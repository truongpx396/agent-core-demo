"""The reconciliation settings (app/core/config.py): the defaults an operator gets, and the bounds that stop a typo becoming a silent
all-clear (a tolerance of 100% reports nothing) or a load on the gateway (a lookback of a year re-reads every spend-log row).

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_ENV = {
    "CREDIT_RECONCILE_TOLERANCE_USD": "tolerance_usd", "CREDIT_RECONCILE_TOLERANCE_PCT": "tolerance_pct",
    "CREDIT_RECONCILE_SETTLE_SECONDS": "settle", "CREDIT_RECONCILE_LOOKBACK_DAYS": "lookback",
    "CREDIT_RECONCILE_INTERVAL_SECONDS": "interval", "CREDIT_RECONCILE_GATEWAY_MAX_PAGES": "pages",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_defaults_are_a_cent_a_percent_a_quarter_hour_two_days_and_six_hours():
    settings = Settings(_env_file=None)

    assert settings.credit_reconcile_tolerance_usd == Decimal("0.01")
    assert settings.credit_reconcile_tolerance_pct == Decimal("1")
    assert settings.credit_reconcile_settle_seconds == 900
    assert settings.credit_reconcile_lookback_days == 2
    assert settings.credit_reconcile_interval_seconds == 6 * 3600
    assert settings.credit_reconcile_gateway_max_pages == 200


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CREDIT_RECONCILE_TOLERANCE_USD", "-0.01"),
        ("CREDIT_RECONCILE_TOLERANCE_PCT", "-1"),
        ("CREDIT_RECONCILE_TOLERANCE_PCT", "100.5"),  # above 100% allows any difference: no check at all
        ("CREDIT_RECONCILE_SETTLE_SECONDS", "-1"),
        ("CREDIT_RECONCILE_LOOKBACK_DAYS", "0"),
        ("CREDIT_RECONCILE_LOOKBACK_DAYS", "36"),  # past a spend log's practical window: a load on the gateway database
        ("CREDIT_RECONCILE_INTERVAL_SECONDS", "59"),  # a pass re-reads the whole window: not every few seconds
        ("CREDIT_RECONCILE_GATEWAY_MAX_PAGES", "0"),
    ],
)
def test_a_value_outside_its_bounds_refuses_to_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CREDIT_RECONCILE_TOLERANCE_USD", "0"),
        ("CREDIT_RECONCILE_TOLERANCE_PCT", "0"),
        ("CREDIT_RECONCILE_TOLERANCE_PCT", "100"),
        ("CREDIT_RECONCILE_SETTLE_SECONDS", "0"),
        ("CREDIT_RECONCILE_LOOKBACK_DAYS", "1"),
        ("CREDIT_RECONCILE_LOOKBACK_DAYS", "35"),
        ("CREDIT_RECONCILE_INTERVAL_SECONDS", "60"),
        ("CREDIT_RECONCILE_GATEWAY_MAX_PAGES", "1"),
    ],
)
def test_the_bounds_themselves_are_allowed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    Settings(_env_file=None)
