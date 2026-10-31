"""`app/adapters/postgres/usage_events.py`: the statements the usage-event store SENDS, against a scripted connection.

That proves the shape (the delete permission is set first and local to the transaction, no tenant narrows the sweep, the table's missing migration
is translated); what the database DOES with them (the PRIMARY KEY's `ON CONFLICT`, the append-only trigger, the batch counts) is proven against a real
Postgres in tests/integration/test_usage_events_real_postgres.py and test_usage_events_retention_real_postgres.py. The application's use of
the store is tested against a fake, in tests/agent/cost/."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from psycopg import errors as pg_errors

from app.adapters.postgres import usage_events as adapter
from app.ports.cost import UsageTableMissing


class _Cursor:
    def __init__(self, row, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row


class _Connection:
    """Answers each statement from a script: a row, or an exception to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "set_config" in sql:
            return _Cursor(("on",))
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Cursor(answer)


def _use(monkeypatch, script):
    connection = _Connection(script)

    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(adapter, "get_connection", get_connection)
    return connection


async def test_each_batch_is_allowed_to_delete_inside_its_own_transaction_and_before_it_deletes(monkeypatch):
    conn = _use(monkeypatch, [(0,)])

    await adapter.PgUsageEventStore().delete_older_than(days=400, limit=10)

    assert "usage_events.allow_delete" in conn.calls[0][0] and "true" in conn.calls[0][0], "LOCAL to the transaction, set first"
    assert "DELETE FROM usage_events" in conn.calls[1][0]


async def test_the_statement_names_no_billing_table_because_billing_keeps_its_own_record_with_its_own_sweep(monkeypatch):
    conn = _use(monkeypatch, [(0,)])

    await adapter.PgUsageEventStore().delete_older_than(days=400, limit=10)

    delete = conn.calls[1][0]
    assert "billing" not in delete and "outbox" not in delete


async def test_the_cutoff_is_a_parameter_and_no_tenant_narrows_it(monkeypatch):
    """Retention is a property of the table, not of one tenant's data: this is an operator job and spans tenants on purpose."""
    conn = _use(monkeypatch, [(0,)])

    gone = await adapter.PgUsageEventStore().delete_older_than(days=123, limit=7)

    sql, params = conn.calls[1]
    assert params == {"days": 123, "n": 7} and gone == 0
    assert "tenant" not in sql


@pytest.mark.parametrize("error", [pg_errors.UndefinedTable("no table"), pg_errors.UndefinedColumn("no column")])
async def test_a_migration_that_was_not_applied_is_the_ports_error_not_psycopgs(monkeypatch, error):
    async def boom(row):
        raise error

    monkeypatch.setattr(adapter, "_insert", boom)

    with pytest.raises(UsageTableMissing):
        await adapter.PgUsageEventStore().insert({"event_id": "e"})  # type: ignore[typeddict-item] - the row never reaches a statement


async def test_any_other_database_error_is_not_translated(monkeypatch):
    async def boom(row):
        raise pg_errors.OperationalError("down")

    monkeypatch.setattr(adapter, "_insert", boom)

    with pytest.raises(pg_errors.OperationalError):
        await adapter.PgUsageEventStore().insert({"event_id": "e"})  # type: ignore[typeddict-item] - the row never reaches a statement


class TestTheInsertStatement:
    """What it says, through a fake connection. That it WORKS against a unique key is the integration test."""

    @staticmethod
    def _fake_connection(monkeypatch, rowcount):
        calls = []

        class Conn:
            async def execute(self, sql, params):
                calls.append((sql, params))
                return SimpleNamespace(rowcount=rowcount)

        @asynccontextmanager
        async def get_connection():
            yield Conn()

        monkeypatch.setattr(adapter, "get_connection", get_connection)
        return calls

    ROW = {
        "event_id": "e1", "tenant": "acme", "principal": "alice", "thread_id": "t", "kind": "chat",
        "model_alias": "chat", "resolved_model": None, "input_tokens": 1, "output_tokens": 2,
        "cached_input_tokens": 0, "total_tokens": 3, "cost_usd": 0.5,
        "price_input_per_token": 0.1, "price_output_per_token": 0.2,
    }

    async def test_it_inserts_with_on_conflict_do_nothing_on_the_event_id(self, monkeypatch):
        calls = self._fake_connection(monkeypatch, rowcount=1)
        recorded = []

        async def record_usage_in(conn, usage):
            recorded.append(usage)

        monkeypatch.setattr(adapter.billing_port, "record_usage_in", record_usage_in)

        assert await adapter._insert(self.ROW) is True

        (sql, params), = calls
        assert "INSERT INTO usage_events" in sql
        assert "ON CONFLICT (event_id) DO NOTHING" in sql
        assert params == self.ROW
        assert [u.event_id for u in recorded] == ["e1"], "a newly written event is handed to billing in the same transaction"

    async def test_a_duplicate_reports_false_and_is_not_handed_to_billing(self, monkeypatch):
        self._fake_connection(monkeypatch, rowcount=0)

        async def record_usage_in(conn, usage):
            raise AssertionError("a duplicate must not be billed twice")

        monkeypatch.setattr(adapter.billing_port, "record_usage_in", record_usage_in)

        assert await adapter._insert(self.ROW) is False
