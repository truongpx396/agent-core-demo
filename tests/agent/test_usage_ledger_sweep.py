"""`usage_ledger.sweep_old_rows` — the retention delete (spec 008 A3).

Fake-connection tests: they pin the batching and the floor, not how a real table
behaves; tests/integration/test_ledger_real_postgres.py runs the same function
against real rows.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent import usage_ledger
from app.core.config import USAGE_LEDGER_MIN_RETENTION_DAYS, Settings


class _Conn:
    def __init__(self, rowcounts):
        self._rowcounts = iter(rowcounts)
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, params):
        self.calls.append((sql, tuple(params)))
        return SimpleNamespace(rowcount=next(self._rowcounts))


def _use(monkeypatch, conn):
    @asynccontextmanager
    async def get_connection():
        yield conn

    monkeypatch.setattr(usage_ledger, "get_connection", get_connection)


async def test_deletes_in_batches_until_a_short_batch_and_returns_the_total(monkeypatch):
    conn = _Conn([5000, 5000, 120])
    _use(monkeypatch, conn)

    deleted = await usage_ledger.sweep_old_rows(older_than_days=400)

    assert deleted == 10120
    assert len(conn.calls) == 3


async def test_an_empty_ledger_is_one_statement_and_zero(monkeypatch):
    conn = _Conn([0])
    _use(monkeypatch, conn)

    assert await usage_ledger.sweep_old_rows(older_than_days=400) == 0
    assert len(conn.calls) == 1


async def test_each_batch_is_bound_by_the_cutoff_and_the_batch_size(monkeypatch):
    conn = _Conn([7])
    _use(monkeypatch, conn)

    await usage_ledger.sweep_old_rows(older_than_days=90, batch_size=500)

    sql, params = conn.calls[0]
    assert "recorded_at < now() - make_interval(days => %s)" in sql
    assert "LIMIT %s" in sql
    assert params == (90, 500)


@pytest.mark.parametrize("days", [0, 1, USAGE_LEDGER_MIN_RETENTION_DAYS - 1])
async def test_a_retention_below_the_floor_raises_before_touching_the_database(monkeypatch, days):
    async def fail(*args, **kwargs):
        raise AssertionError("nothing may be deleted when the retention is below the floor")

    monkeypatch.setattr(usage_ledger, "get_connection", fail)

    with pytest.raises(ValueError, match="floor"):
        await usage_ledger.sweep_old_rows(older_than_days=days)


async def test_the_floor_itself_is_allowed(monkeypatch):
    _use(monkeypatch, _Conn([0]))

    assert await usage_ledger.sweep_old_rows(older_than_days=USAGE_LEDGER_MIN_RETENTION_DAYS) == 0


def test_the_floor_clears_a_calendar_month():
    assert USAGE_LEDGER_MIN_RETENTION_DAYS > 31


def test_the_retention_setting_rejects_a_value_below_the_floor():
    with pytest.raises(ValueError):
        Settings(usage_ledger_retention_days=USAGE_LEDGER_MIN_RETENTION_DAYS - 1)

    assert Settings(usage_ledger_retention_days=USAGE_LEDGER_MIN_RETENTION_DAYS).usage_ledger_retention_days == (
        USAGE_LEDGER_MIN_RETENTION_DAYS
    )
