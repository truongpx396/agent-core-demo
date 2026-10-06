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


class _RunawayConn:
    """Every batch comes back full, forever; raises past `limit` calls so a loop with no
    ceiling fails this test instead of hanging the suite."""

    def __init__(self, rows_per_batch, limit):
        self._rows = rows_per_batch
        self._limit = limit
        self.calls = 0

    async def execute(self, sql, params):
        self.calls += 1
        assert self.calls <= self._limit, "the sweep has no ceiling: it was still deleting after the limit"
        return SimpleNamespace(rowcount=self._rows)


async def test_one_run_stops_at_its_batch_ceiling_even_if_every_batch_is_full(monkeypatch, caplog):
    """The app never writes a row older than the cutoff (`recorded_at` defaults to now()), so a sweep
    normally ends at the first short batch. A restore or backfill that keeps re-inserting old rows
    would make "until a short batch" unbounded: one run must have its own ceiling, say so when it
    hits it (the next run continues), and never loop on its own."""
    conn = _RunawayConn(rows_per_batch=500, limit=50)
    _use(monkeypatch, conn)

    with caplog.at_level("WARNING", logger=usage_ledger.logger.name):
        deleted = await usage_ledger.sweep_old_rows(older_than_days=90, batch_size=500, max_batches=3)

    assert conn.calls == 3
    assert deleted == 1500
    assert any(r.message == "usage_ledger_sweep_hit_batch_ceiling" for r in caplog.records)


async def test_the_default_ceiling_is_finite(monkeypatch):
    conn = _RunawayConn(rows_per_batch=usage_ledger.SWEEP_BATCH_SIZE, limit=usage_ledger.SWEEP_MAX_BATCHES + 5)
    _use(monkeypatch, conn)

    await usage_ledger.sweep_old_rows(older_than_days=400)

    assert conn.calls == usage_ledger.SWEEP_MAX_BATCHES


async def test_finishing_under_the_ceiling_does_not_warn(monkeypatch, caplog):
    _use(monkeypatch, _Conn([500, 500, 3]))

    with caplog.at_level("WARNING", logger=usage_ledger.logger.name):
        await usage_ledger.sweep_old_rows(older_than_days=90, batch_size=500, max_batches=3)

    assert not [r for r in caplog.records if r.message == "usage_ledger_sweep_hit_batch_ceiling"]


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
