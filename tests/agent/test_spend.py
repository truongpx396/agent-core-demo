"""`app/agent/spend.py`: the spend read behind every dollar cap and `GET /usage` (specs/010 T030).

Same "no live Postgres" approach as tests/agent/test_usage_ledger.py: `get_connection` is replaced by a fake that records
the SQL and parameters it was handed. That proves statement SHAPE (tenant in every query, which table, which column the
window uses), not how the real table behaves: tests/integration/test_spend_real_postgres.py runs the same read against it.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.agent import budgets, spend, usage_ledger
from tests.conftest import TEST_CTX, _NoPostgresInTests


class _Cursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row=(0, 0)):
        self.calls: list[tuple[str, list]] = []
        self._row = row

    async def execute(self, sql, params):
        self.calls.append((sql, list(params)))
        return _Cursor(self._row)


def _use(monkeypatch, fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    monkeypatch.setattr(spend, "get_connection", get_connection)


async def test_it_reads_the_usage_events_and_never_the_ledger(monkeypatch):
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme")

    (sql, _), = fake.calls
    assert "FROM usage_events" in sql
    assert "usage_ledger" not in sql


async def test_the_tenant_is_a_parameter_of_every_statement(monkeypatch):
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme")
    await spend.usage_summary("acme", principal="alice")
    await spend.usage_summary("acme", principal="alice", since=datetime.now(UTC))

    for sql, params in fake.calls:
        assert "tenant = %s" in sql and params[0] == "acme"


async def test_a_person_narrows_the_sum_with_their_own_parameter(monkeypatch):
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme", principal="alice")

    (sql, params), = fake.calls
    assert "principal = %s" in sql and params == ["acme", "alice"]


async def test_no_principal_means_the_whole_tenant(monkeypatch):
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme")

    (sql, params), = fake.calls
    assert "principal" not in sql and params == ["acme"]


async def test_the_window_is_the_time_of_the_call_not_the_time_it_was_stored(monkeypatch):
    """`occurred_at` is what `(tenant, occurred_at)` indexes; a turn is counted in the window it ran in."""
    fake = _Connection()
    _use(monkeypatch, fake)
    since = datetime.now(UTC) - timedelta(hours=24)

    await spend.usage_summary("acme", since=since)

    (sql, params), = fake.calls
    assert "occurred_at >= %s" in sql and "recorded_at" not in sql
    assert params == ["acme", since]


async def test_an_all_time_read_has_no_time_bound(monkeypatch):
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme")

    (sql, _), = fake.calls
    assert "occurred_at" not in sql


async def test_an_unpriced_call_cannot_turn_the_sum_null(monkeypatch):
    """cost_usd is NULL for an unpriced call (unknown, not free). Shape only: the real behaviour is in the integration tier."""
    fake = _Connection()
    _use(monkeypatch, fake)

    await spend.usage_summary("acme")

    (sql, _), = fake.calls
    assert "COALESCE(SUM(cost_usd), 0)" in sql and "COALESCE(SUM(total_tokens), 0)" in sql


async def test_it_returns_plain_numbers_from_what_postgres_sends(monkeypatch):
    _use(monkeypatch, _Connection(row=(1234, Decimal("0.250000000000"))))

    summary = await spend.usage_summary("acme")

    assert summary == {"total_tokens": 1234, "total_cost_usd": 0.25}
    assert isinstance(summary["total_tokens"], int) and isinstance(summary["total_cost_usd"], float)


async def test_a_database_error_is_raised_not_read_as_zero(monkeypatch):
    """A cap that silently reads $0 is a cap that is off: the caller (`budgets`) owns the failure policy."""

    @asynccontextmanager
    async def broken():
        raise ConnectionError("appdata postgres unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(spend, "get_connection", broken)

    with pytest.raises(ConnectionError):
        await spend.usage_summary("acme")


def test_the_ledger_no_longer_answers_this_question():
    """A stale caller must fail loudly (AttributeError) instead of quietly reading the frozen per-turn table."""
    assert not hasattr(usage_ledger, "usage_summary")


async def test_a_cap_reads_the_events_through_this_module(monkeypatch):
    """The wiring: `budgets` asks `spend`, so events summed here refuse a turn. Under the old ledger read this goes
    through `usage_ledger.get_connection`, which the suite's autouse guard makes raise, so the check would FAIL OPEN
    and the turn would be served: this test fails without the change."""
    _use(monkeypatch, _Connection(row=(900, Decimal("6.50"))))

    async def no_holds(tenant):
        return 0.0

    monkeypatch.setattr(usage_ledger, "in_flight_reservation", no_holds)

    allowance = await budgets.check_allowance(
        TEST_CTX, limits=[budgets.BudgetLimit("tenant", "day", 5.0)], fail_policy="open"
    )

    assert allowance.status == "exceeded"
    assert allowance.spent_usd == pytest.approx(6.50) and allowance.degraded is False


async def test_the_suites_own_guard_covers_this_read():
    """`mock_appdata_postgres` (tests/conftest.py) must stop THIS module reaching a real database: the read runs before
    every turn, so a turn test that is not guarded pays a real connection attempt for it and its result depends on what is
    running on the machine. No monkeypatching here, on purpose: this is the guard as every other test sees it."""
    with pytest.raises(_NoPostgresInTests):
        await spend.usage_summary("acme")
