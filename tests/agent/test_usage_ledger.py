"""Tests for app/agent/usage_ledger.py's in-flight budget reservation
(reserve_budget/release_budget_reservation/in_flight_reservation) — the fix
for the check-then-act race in app/agent/budgets.py::check_tenant_daily
(tests/agent/test_tenant_budget.py covers that function itself; this file
covers the reservation primitives it now reads).

Same "no live Postgres" approach as tests/agent/test_sql_store.py:
get_connection is monkeypatched to a fake async-context-manager connection
that records the SQL/params it was called with. That proves statement SHAPE
(tenant scoping, which columns, the staleness cutoff), not how the real
database behaves — tests/integration/test_budget_holds_real_postgres.py
drives the real table, including the abandoned-hold cases this file cannot.
"""
import uuid
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
    async def test_inserts_one_hold_for_this_tenant_and_returns_its_id(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        hold_id = await usage_ledger.reserve_budget(TEST_CTX, 0.5)

        insert_sql, insert_params = fake.calls[-1]
        assert "INSERT INTO tenant_budget_holds" in insert_sql
        assert insert_params == [hold_id, TEST_CTX["tenant"], 0.5]
        uuid.UUID(hold_id)  # a real UUID, safe for the UUID column

    async def test_each_reservation_is_its_own_hold(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        first = await usage_ledger.reserve_budget(TEST_CTX, 0.5)
        second = await usage_ledger.reserve_budget(TEST_CTX, 0.5)

        assert first != second

    async def test_sweeps_only_this_tenants_abandoned_holds_before_inserting(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        await usage_ledger.reserve_budget(TEST_CTX, 0.5)

        sweep_sql, sweep_params = fake.calls[0]
        assert sweep_sql.startswith("DELETE FROM tenant_budget_holds")
        assert "tenant = %s" in sweep_sql and "created_at <=" in sweep_sql
        assert sweep_params == [TEST_CTX["tenant"], usage_ledger.RESERVATION_STALE_AFTER_MINUTES]

    async def test_none_for_an_invalid_ctx_without_touching_the_database(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for an invalid ctx")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        assert await usage_ledger.reserve_budget(None, 0.5) is None

    async def test_none_for_a_non_positive_amount(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for amount <= 0")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        assert await usage_ledger.reserve_budget(TEST_CTX, 0.0) is None

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
        assert await usage_ledger.reserve_budget(TEST_CTX, 0.5) is None


class TestReleaseBudgetReservation:
    async def test_deletes_exactly_this_hold_scoped_to_the_tenant(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        await usage_ledger.release_budget_reservation(TEST_CTX, "hold-1")

        sql, params = fake.calls[0]
        assert sql.startswith("DELETE FROM tenant_budget_holds")
        assert "hold_id = %s" in sql and "tenant = %s" in sql
        assert params == ["hold-1", TEST_CTX["tenant"]]

    async def test_no_hold_id_is_a_noop(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached without a hold id")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        await usage_ledger.release_budget_reservation(TEST_CTX, None)  # must not raise

    async def test_an_invalid_ctx_is_a_noop(self, monkeypatch):
        async def _fail_if_called():
            raise AssertionError("get_connection should not be reached for an invalid ctx")

        monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)
        await usage_ledger.release_budget_reservation(None, "hold-1")  # must not raise

    async def test_never_raises_even_if_the_write_fails(self, monkeypatch):
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "get_connection", _broken)
        await usage_ledger.release_budget_reservation(TEST_CTX, "hold-1")  # must not raise


class TestInFlightReservation:
    async def test_returns_the_summed_fresh_holds(self, monkeypatch):
        fake = _FakeConnection(fetchone_result=(0.75,))
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        assert await usage_ledger.in_flight_reservation("ecorp") == 0.75

    async def test_zero_when_there_is_no_row_or_a_null_sum(self, monkeypatch):
        for result in (None, (None,)):
            fake = _FakeConnection(fetchone_result=result)
            monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

            assert await usage_ledger.in_flight_reservation("ecorp") == 0.0

    async def test_excludes_an_abandoned_hold_at_the_query_level(self, monkeypatch):
        """A worker that died mid-turn without releasing must not
        permanently inflate this tenant's apparent spend — the staleness
        cutoff is a WHERE clause on each hold's OWN timestamp, so an
        abandoned hold simply doesn't match, whatever the tenant does next."""
        fake = _FakeConnection(fetchone_result=None)
        monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

        await usage_ledger.in_flight_reservation("ecorp")

        sql, params = fake.calls[0]
        assert "SUM(reserved_usd)" in sql
        assert "tenant = %s" in sql and "created_at >" in sql
        assert params == ["ecorp", usage_ledger.RESERVATION_STALE_AFTER_MINUTES]

    async def test_fails_open_to_zero_when_the_read_raises(self, monkeypatch):
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "get_connection", _broken)
        assert await usage_ledger.in_flight_reservation("ecorp") == 0.0
