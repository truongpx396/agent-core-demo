"""`USAGE_EVENT_RETENTION_DAYS` (app/core/config.py): below the floor a sweep would delete spend that a window still reads.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
import pytest
from pydantic import ValidationError

from app.core.config import USAGE_EVENT_MIN_RETENTION_DAYS, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("USAGE_EVENT_RETENTION_DAYS", raising=False)


def test_the_default_keeps_a_year_and_then_some():
    assert Settings(_env_file=None).usage_event_retention_days == 400


def test_the_floor_covers_every_window_that_reads_the_events():
    """A monthly cap reads back 31 days, the reconciliation up to 35, the export outbox gives up at 30."""
    assert USAGE_EVENT_MIN_RETENTION_DAYS >= 35


def test_the_floor_itself_is_accepted(monkeypatch):
    monkeypatch.setenv("USAGE_EVENT_RETENTION_DAYS", str(USAGE_EVENT_MIN_RETENTION_DAYS))

    assert Settings(_env_file=None).usage_event_retention_days == USAGE_EVENT_MIN_RETENTION_DAYS


def test_less_than_the_floor_refuses_to_start(monkeypatch):
    monkeypatch.setenv("USAGE_EVENT_RETENTION_DAYS", str(USAGE_EVENT_MIN_RETENTION_DAYS - 1))

    with pytest.raises(ValidationError, match="usage_event_retention_days"):
        Settings(_env_file=None)
