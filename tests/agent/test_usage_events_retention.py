"""`app/agent/usage_events_retention.py`: the shape and the control flow of the usage-event retention sweep (specs/010 T030c3).

Hermetic, with a scripted connection: that proves what is sent (the floor, the delete permission, which events are never
touched) and when the loop stops. What the database DOES with the statements (the foreign key, the append-only trigger, the batch
counts) is proven against a real Postgres in tests/integration/test_usage_events_retention_real_postgres.py."""
from contextlib import asynccontextmanager

import pytest
from psycopg import errors as pg_errors

from app.agent import usage_events_retention as retention
from app.core.config import USAGE_EVENT_MIN_RETENTION_DAYS


class _Cursor:
    def __init__(self, row):
        self._row = row

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
    connections: list[_Connection] = []
    pending = list(script)

    @asynccontextmanager
    async def get_connection():
        # one scripted answer per connection, in the order the sweep asks
        conn = _Connection([pending.pop(0)])
        connections.append(conn)
        yield conn

    monkeypatch.setattr(retention, "get_connection", get_connection)
    return connections


def _statements(connections):
    return [sql for conn in connections for sql, _ in conn.calls if "set_config" not in sql]


async def test_fewer_days_than_the_floor_is_refused_before_the_database_is_touched(monkeypatch):
    def never():
        raise AssertionError("the floor must be checked before any connection is opened")

    monkeypatch.setattr(retention, "get_connection", never)

    with pytest.raises(ValueError, match="floor"):
        await retention.sweep_old_events(older_than_days=USAGE_EVENT_MIN_RETENTION_DAYS - 1)


async def test_the_floor_itself_is_allowed(monkeypatch):
    _use(monkeypatch, [(0, 0), (0,)])

    result = await retention.sweep_old_events(older_than_days=USAGE_EVENT_MIN_RETENTION_DAYS)

    assert result.complete is True and result.deleted == 0


async def test_each_batch_is_allowed_to_delete_inside_its_own_transaction_and_before_it_deletes(monkeypatch):
    connections = _use(monkeypatch, [(0, 0), (0,)])

    await retention.sweep_old_events(older_than_days=400)

    batch = connections[0]
    assert "usage_events.allow_delete" in batch.calls[0][0] and "true" in batch.calls[0][0], "LOCAL to the transaction, set first"
    assert "DELETE FROM usage_events" in batch.calls[1][0]


async def test_an_event_whose_export_did_not_finish_is_never_a_candidate(monkeypatch):
    connections = _use(monkeypatch, [(0, 0), (0,)])

    await retention.sweep_old_events(older_than_days=400)

    batch_sql = _statements(connections)[0]
    assert "o.status <> 'sent'" in batch_sql and "NOT EXISTS" in batch_sql
    assert "status = 'pending'" not in batch_sql, "pending is only one of the three unfinished states: failed and expired count too"


async def test_a_sent_event_goes_with_its_export_row_in_one_statement_because_the_constraint_is_checked_at_its_end(monkeypatch):
    connections = _use(monkeypatch, [(0, 0), (0,)])

    await retention.sweep_old_events(older_than_days=400)

    batch_sql = _statements(connections)[0]
    assert "DELETE FROM usage_export_outbox" in batch_sql and "DELETE FROM usage_events" in batch_sql


async def test_the_cutoff_is_a_parameter_and_no_tenant_narrows_it(monkeypatch):
    """Retention is a property of the table, not of one tenant's data: this is an operator job and spans tenants on purpose."""
    connections = _use(monkeypatch, [(0, 0), (0,)])

    await retention.sweep_old_events(older_than_days=123, batch_size=7)

    sql, params = connections[0].calls[1]
    assert params == {"days": 123, "n": 7}
    assert "tenant" not in sql


async def test_it_stops_at_the_first_short_batch_and_adds_up_the_batches(monkeypatch):
    _use(monkeypatch, [(2, 1), (2, 1), (1, 0), (4,)])

    result = await retention.sweep_old_events(older_than_days=400, batch_size=2)

    assert (result.deleted, result.outbox_cleared, result.held_back, result.complete) == (5, 2, 4, True)


async def test_a_run_that_hits_its_ceiling_says_it_is_not_complete(monkeypatch):
    _use(monkeypatch, [(2, 0), (2, 0), (0,)])

    result = await retention.sweep_old_events(older_than_days=400, batch_size=2, max_batches=2)

    assert result.deleted == 4 and result.complete is False


async def test_a_deployment_without_the_outbox_table_still_sweeps_and_has_nothing_held_back(monkeypatch):
    connections = _use(monkeypatch, [pg_errors.UndefinedTable('relation "usage_export_outbox" does not exist'), (3, 0)])

    result = await retention.sweep_old_events(older_than_days=400, batch_size=10)

    assert (result.deleted, result.outbox_cleared, result.held_back, result.complete) == (3, 0, 0, True)
    assert "usage_export_outbox" in _statements(connections)[0]
    assert "usage_export_outbox" not in _statements(connections)[1]


async def test_a_missing_events_table_is_an_error_not_an_empty_sweep(monkeypatch):
    _use(monkeypatch, [pg_errors.UndefinedTable("a"), pg_errors.UndefinedTable("b")])

    with pytest.raises(pg_errors.UndefinedTable):
        await retention.sweep_old_events(older_than_days=400)
