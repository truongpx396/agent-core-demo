"""Tests for app/domains/ops/store.py's query construction — same "assert
the SQL text/params, no live Postgres" approach as
tests/domains/support/test_store.py / tests/domains/sales/test_store.py.
Unlike those, this table carries no `tenant` column (see
postgres-init/10-ops-incidents.sql's own comment for why), so there's no
tenant-scoping assertion to make here — only `opened_by` attribution and
the status/limit filtering.
"""
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

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, row=None, rows=None, rowcount=0, columns=()):
        self.captured = {}
        self._row = row
        self._rows = rows
        self._rowcount = rowcount
        self._columns = columns

    def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._row, self._rows, self._rowcount, self._columns)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_log_incident_returns_the_new_id(monkeypatch):
    fake = _FakeConnection(row=(1,))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    incident_id = store.log_incident("ops-user", "latency spike", "p95 at 45s")

    assert incident_id == 1
    assert fake.captured["params"] == ["ops-user", "latency spike", "p95 at 45s"]


def test_list_recent_incidents_defaults_to_no_status_filter(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "ops-user", "latency spike", None, "open", None, "t", None)],
        columns=_INCIDENT_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    incidents = store.list_recent_incidents()

    assert incidents[0]["summary"] == "latency spike"
    assert fake.captured["params"] == [10]
    assert "WHERE" not in fake.captured["sql"]


def test_list_recent_incidents_filters_by_status_when_given(monkeypatch):
    fake = _FakeConnection(rows=[], columns=_INCIDENT_COLUMNS)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    store.list_recent_incidents(limit=5, status="open")

    assert fake.captured["params"] == ["open", 5]
    assert "WHERE status = %s" in fake.captured["sql"]


def test_resolve_incident_returns_false_when_no_row_updated(monkeypatch):
    fake = _FakeConnection(rowcount=0)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.resolve_incident(999, "fixed") is False


def test_resolve_incident_returns_true_and_sets_resolution(monkeypatch):
    fake = _FakeConnection(rowcount=1)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.resolve_incident(1, "restarted the worker") is True
    assert fake.captured["params"] == ["restarted the worker", 1]
