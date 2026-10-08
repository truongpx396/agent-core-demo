"""`USAGE_EVENTS_ENABLED` (app/core/config.py): the events are the spend the dollar caps sum (specs/010 T030), so
the old emergency kill switch would now turn every cap off without a sound. It is refused at startup instead.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("USAGE_EVENTS_ENABLED", raising=False)


def test_the_events_are_on_by_default():
    assert Settings(_env_file=None).usage_events_enabled is True


def test_an_explicit_true_is_accepted(monkeypatch):
    monkeypatch.setenv("USAGE_EVENTS_ENABLED", "true")

    assert Settings(_env_file=None).usage_events_enabled is True


@pytest.mark.parametrize("off", ["false", "0", "no", "off"])
def test_switching_the_events_off_refuses_to_start_because_it_would_blind_every_cap(monkeypatch, off):
    monkeypatch.setenv("USAGE_EVENTS_ENABLED", off)

    with pytest.raises(ValidationError, match="every cap would read"):
        Settings(_env_file=None)
