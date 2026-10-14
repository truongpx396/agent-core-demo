"""The gateway-reconciliation settings (app/core/config.py): the defaults an operator gets, and the bounds that stop a typo becoming a silent
all-clear (a tolerance of 100% reports nothing) or a load on the gateway (a lookback of a year re-reads every spend-log row). They were
`USAGE_RECONCILE_*` while the same job also checked the wallet; the old names must keep working, so an existing `.env` is not silently ignored.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_FIELDS = {"TOLERANCE_USD": "tolerance_usd", "TOLERANCE_PCT": "tolerance_pct", "SETTLE_SECONDS": "settle", "LOOKBACK_DAYS": "lookback",
           "INTERVAL_SECONDS": "interval", "GATEWAY_MAX_PAGES": "pages"}
_ENV = [f"{prefix}_RECONCILE_{name}" for name in _FIELDS for prefix in ("USAGE", "CREDIT")]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_defaults_are_a_cent_a_percent_a_quarter_hour_two_days_and_six_hours():
    settings = Settings(_env_file=None)

    assert settings.usage_reconcile_tolerance_usd == Decimal("0.01")
    assert settings.usage_reconcile_tolerance_pct == Decimal("1")
    assert settings.usage_reconcile_settle_seconds == 900
    assert settings.usage_reconcile_lookback_days == 2
    assert settings.usage_reconcile_interval_seconds == 6 * 3600
    assert settings.usage_reconcile_gateway_max_pages == 200


def test_the_old_credit_names_are_still_read_so_an_existing_env_file_keeps_its_values(monkeypatch):
    monkeypatch.setenv("USAGE_RECONCILE_TOLERANCE_USD", "0.5")
    monkeypatch.setenv("USAGE_RECONCILE_GATEWAY_MAX_PAGES", "7")

    settings = Settings(_env_file=None)

    assert settings.usage_reconcile_tolerance_usd == Decimal("0.5") and settings.usage_reconcile_gateway_max_pages == 7


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("USAGE_RECONCILE_LOOKBACK_DAYS", "9")
    monkeypatch.setenv("USAGE_RECONCILE_LOOKBACK_DAYS", "3")

    assert Settings(_env_file=None).usage_reconcile_lookback_days == 3


def test_a_field_can_still_be_set_by_its_own_name():
    assert Settings(_env_file=None, usage_reconcile_lookback_days=5).usage_reconcile_lookback_days == 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("USAGE_RECONCILE_TOLERANCE_USD", "-0.01"),
        ("USAGE_RECONCILE_TOLERANCE_PCT", "-1"),
        ("USAGE_RECONCILE_TOLERANCE_PCT", "100.5"),  # above 100% allows any difference: no check at all
        ("USAGE_RECONCILE_SETTLE_SECONDS", "-1"),
        ("USAGE_RECONCILE_LOOKBACK_DAYS", "0"),
        ("USAGE_RECONCILE_LOOKBACK_DAYS", "36"),  # past a spend log's practical window: a load on the gateway database
        ("USAGE_RECONCILE_INTERVAL_SECONDS", "59"),  # a pass re-reads the whole window: not every few seconds
        ("USAGE_RECONCILE_GATEWAY_MAX_PAGES", "0"),
    ],
)
def test_a_value_outside_its_bounds_refuses_to_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("USAGE_RECONCILE_TOLERANCE_USD", "0"),
        ("USAGE_RECONCILE_TOLERANCE_PCT", "0"),
        ("USAGE_RECONCILE_TOLERANCE_PCT", "100"),
        ("USAGE_RECONCILE_SETTLE_SECONDS", "0"),
        ("USAGE_RECONCILE_LOOKBACK_DAYS", "1"),
        ("USAGE_RECONCILE_LOOKBACK_DAYS", "35"),
        ("USAGE_RECONCILE_INTERVAL_SECONDS", "60"),
        ("USAGE_RECONCILE_GATEWAY_MAX_PAGES", "1"),
    ],
)
def test_the_bounds_themselves_are_allowed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    Settings(_env_file=None)
