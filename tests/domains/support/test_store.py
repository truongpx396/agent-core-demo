"""Tests for app/domains/support/store.py's query construction — same
"assert the SQL text/params, no live Postgres" approach as
tests/agent/test_sql_store.py.
"""
from app.domains.support import store


class _FakeCursor:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount
        self.description = [type("Col", (), {"name": c}) for c in (
            "id", "tenant", "requester", "subject", "description", "priority",
            "status", "escalation_reason", "created_at", "updated_at",
        )]

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row=None, rowcount=0):
        self.captured = {}
        self._row = row
        self._rowcount = rowcount

    def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._row, self._rowcount)

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
    fake = _FakeConnection(row=(1, "acme", "alice", "s", "d", "normal", "open", None, "t", "t"))
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
