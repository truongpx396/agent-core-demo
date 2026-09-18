"""Tests for app/domains/ops/store.py's query construction — same "assert
the SQL text/params, no live Postgres" approach as
tests/domains/support/test_store.py / tests/domains/sales/test_store.py.
Unlike those, this table carries no `tenant` column (see
postgres-init/10-ops-incidents.sql's own comment for why), so there's no
tenant-scoping assertion to make here — only `opened_by` attribution and
the status/limit filtering.

Every function here is `async def` now (a real `AsyncConnectionPool`, see
app/agent/sql_store.py's own docstring), so every call below runs through
`asyncio.run(...)`, this repo's established pattern for exercising async
code from a plain `def test_...`.
"""
from contextlib import asynccontextmanager

from app.domains.ops import store

_INCIDENT_COLUMNS = (
    "id", "opened_by", "summary", "detail", "status", "resolution", "created_at", "resolved_at",
)


class _FakeCursor:
    def __init__(self, row=None, rows=None, rowcount=0, columns=()):
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount
        self.description = [type("Col", (), {"name": c}) for c in columns]

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, row=None, rows=None, rowcount=0, columns=()):
        self.captured = {}
        self._row = row
        self._rows = rows
        self._rowcount = rowcount
        self._columns = columns

    async def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._row, self._rows, self._rowcount, self._columns)


def _fake_get_connection(fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    return get_connection


async def test_log_incident_returns_the_new_id(monkeypatch):
    fake = _FakeConnection(row=(1,))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    incident_id = await store.log_incident("ops-user", "latency spike", "p95 at 45s")

    assert incident_id == 1
    assert fake.captured["params"] == ["ops-user", "latency spike", "p95 at 45s"]


async def test_list_recent_incidents_defaults_to_no_status_filter(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "ops-user", "latency spike", None, "open", None, "t", None)],
        columns=_INCIDENT_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    incidents = await store.list_recent_incidents()

    assert incidents[0]["summary"] == "latency spike"
    assert fake.captured["params"] == [10]
    assert "WHERE" not in fake.captured["sql"]


async def test_list_recent_incidents_filters_by_status_when_given(monkeypatch):
    fake = _FakeConnection(rows=[], columns=_INCIDENT_COLUMNS)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.list_recent_incidents(limit=5, status="open")

    assert fake.captured["params"] == ["open", 5]
    assert "WHERE status = %s" in fake.captured["sql"]


async def test_resolve_incident_returns_false_when_no_row_updated(monkeypatch):
    fake = _FakeConnection(rowcount=0)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.resolve_incident(999, "fixed") is False


async def test_resolve_incident_returns_true_and_sets_resolution(monkeypatch):
    fake = _FakeConnection(rowcount=1)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.resolve_incident(1, "restarted the worker") is True
    assert fake.captured["params"] == ["restarted the worker", 1]
