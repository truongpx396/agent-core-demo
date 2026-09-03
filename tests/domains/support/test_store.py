"""Tests for app/domains/support/store.py's query construction — same
"assert the SQL text/params, no live Postgres" approach as
tests/agent/test_sql_store.py.
"""
from app.domains.support import store


class _FakeCursor:
    def __init__(self, row=None, rows=None, rowcount=0, columns=None):
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount
        self.description = [
            type("Col", (), {"name": c})
            for c in (
                columns
                or (
                    "id", "tenant", "requester", "subject", "description", "priority",
                    "status", "escalation_reason", "notes", "created_at", "updated_at",
                )
            )
        ]

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, row=None, rows=None, rowcount=0, columns=None):
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


def test_create_ticket_always_scopes_to_tenant_and_returns_the_new_id(monkeypatch):
    fake = _FakeConnection(row=(42,))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    ticket_id = store.create_ticket("acme", "alice", "Login broken", "Can't log in", "high")

    assert ticket_id == 42
    assert "tenant" in fake.captured["sql"]
    assert fake.captured["params"][0] == "acme"


def test_get_ticket_scopes_to_tenant_and_id(monkeypatch):
    fake = _FakeConnection(row=(1, "acme", "alice", "s", "d", "normal", "open", None, None, "t", "t"))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    ticket = store.get_ticket("acme", 1)

    assert ticket["id"] == 1
    assert fake.captured["params"] == ["acme", 1]


def test_get_ticket_returns_none_for_no_match(monkeypatch):
    fake = _FakeConnection(row=None)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.get_ticket("acme", 999) is None


def test_escalate_ticket_returns_false_when_no_row_updated(monkeypatch):
    fake = _FakeConnection(rowcount=0)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.escalate_ticket("acme", 999, "reason") is False


def test_escalate_ticket_returns_true_and_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection(rowcount=1)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.escalate_ticket("acme", 1, "billing issue") is True
    assert fake.captured["params"] == ["billing issue", "acme", 1]


def test_list_tickets_for_requester_scopes_to_tenant_and_requester(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "Login broken", "high", "open", "t")],
        columns=("id", "subject", "priority", "status", "created_at"),
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    tickets = store.list_tickets_for_requester("acme", "alice")

    assert tickets[0]["subject"] == "Login broken"
    assert fake.captured["params"][:2] == ["acme", "alice"]


def test_list_tickets_for_requester_respects_the_limit_param(monkeypatch):
    fake = _FakeConnection(rows=[], columns=("id", "subject", "priority", "status", "created_at"))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    store.list_tickets_for_requester("acme", "alice", limit=3)

    assert fake.captured["params"] == ["acme", "alice", 3]


def test_add_comment_returns_false_when_no_row_updated(monkeypatch):
    fake = _FakeConnection(rowcount=0)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.add_comment("acme", 999, "still broken") is False


def test_add_comment_returns_true_and_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection(rowcount=1)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.add_comment("acme", 1, "still broken") is True
    assert fake.captured["params"] == ["still broken", "acme", 1]
