"""Tests for app/agent/usage_ledger.py's in-flight budget reservation
(reserve_budget/release_budget_reservation/in_flight_reservation) — the fix
for the check-then-act race in app/agent/runtime.py::_tenant_over_daily_budget
(tests/agent/test_tenant_budget.py covers that function itself; this file
covers the reservation primitives it now reads).

Same "no live Postgres" approach as tests/agent/test_sql_store.py:
get_connection is monkeypatched to a fake async-context-manager connection
that records the SQL/params it was called with.
"""
from contextlib import asynccontextmanager

from app.agent import usage_ledger
from tests.conftest import TEST_CTX


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows


class _FakeConnection:
    def __init__(self, fetchone_result=None):
        self.calls: list[tuple[str, list]] = []
        self._fetchone_result = fetchone_result

    async def execute(self, sql, params):
        self.calls.append((sql, list(params)))
        return _FakeCursor(self._fetchone_result)


def _fake_get_connection(fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    return get_connection


class TestReserveBudget:
    async def test_upserts_the_reservation_for_this_tenant(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        result = await usage_ledger.reserve_budget(TEST_CTX, 0.5)

        assert result is True
        sql, params = fake.calls[0]
        assert "ON CONFLICT" in sql
        assert params == [TEST_CTX["tenant"], 0.5]

    async def test_false_for_an_invalid_ctx_without_touching_the_database(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for an invalid ctx")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        assert await usage_ledger.reserve_budget(None, 0.5) is False

    async def test_false_for_a_non_positive_amount(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for amount <= 0")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        assert await usage_ledger.reserve_budget(TEST_CTX, 0.0) is False

    async def test_fails_open_when_the_write_raises(self, monkeypatch):
        # Plain sync raiser, not `async def` — matches get_connection()'s
        # real call shape (called, not awaited, before `async with` even
        # starts), same idiom as tests/conftest.py's own
        # _no_postgres_in_tests; an `async def` here would construct a
        # coroutine that `async with` never awaits, just a spurious
        # "coroutine was never awaited" warning.
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "get_connection", _broken)
        assert await usage_ledger.reserve_budget(TEST_CTX, 0.5) is False


class TestReleaseBudgetReservation:
    async def test_subtracts_the_amount_floored_at_zero(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        await usage_ledger.release_budget_reservation(TEST_CTX, 0.5)

        sql, params = fake.calls[0]
        assert "GREATEST" in sql
        assert params == [0.5, TEST_CTX["tenant"]]

    async def test_a_non_positive_amount_is_a_noop(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for amount <= 0")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        await usage_ledger.release_budget_reservation(TEST_CTX, 0.0)  # must not raise

    async def test_never_raises_even_if_the_write_fails(self, monkeypatch):
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "get_connection", _broken)
        await usage_ledger.release_budget_reservation(TEST_CTX, 0.5)  # must not raise


class TestInFlightReservation:
    async def test_returns_the_current_reserved_amount(self, monkeypatch):
        fake = _FakeConnection(fetchone_result=(0.75,))
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        assert await usage_ledger.in_flight_reservation("ecorp") == 0.75

    async def test_zero_when_no_row_exists_for_this_tenant(self, monkeypatch):
        fake = _FakeConnection(fetchone_result=None)
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        assert await usage_ledger.in_flight_reservation("ecorp") == 0.0

    async def test_excludes_a_stale_reservation_at_the_query_level(self, monkeypatch):
        """A worker that died mid-turn without releasing must not
        permanently inflate this tenant's apparent spend — the staleness
        cutoff is a WHERE clause, so a stale row simply doesn't match
        rather than needing separate cleanup logic."""
        fake = _FakeConnection(fetchone_result=None)
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        await usage_ledger.in_flight_reservation("ecorp")

        sql, params = fake.calls[0]
        assert "updated_at >" in sql
        assert usage_ledger.RESERVATION_STALE_AFTER_MINUTES in params

    async def test_fails_open_to_zero_when_the_read_raises(self, monkeypatch):
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "get_connection", _broken)
        assert await usage_ledger.in_flight_reservation("ecorp") == 0.0
