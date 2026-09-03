"""Tests for app/domains/sales/store.py's query construction — same
"assert the SQL text/params, no live Postgres" approach as
tests/agent/test_sql_store.py / tests/domains/support/test_store.py.

`add_followup`/`lead_history` each issue TWO queries — first `get_lead`,
then their own — so those two are tested with `get_lead` itself
monkeypatched (a real, public function this module already exposes)
rather than juggling a multi-call fake connection.
"""
from app.domains.sales import store


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


_LEAD_COLUMNS = ("id", "tenant", "name", "contact", "status", "notes", "created_at", "updated_at")


def test_find_or_create_lead_always_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection(row=(1,))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    lead_id = store.find_or_create_lead("acme", "Jordan", "jordan@example.com", "asked about pricing")

    assert lead_id == 1
    assert fake.captured["params"][0] == "acme"


def test_get_lead_scopes_to_tenant_and_contact(monkeypatch):
    fake = _FakeConnection(
        row=(1, "acme", "Jordan", "jordan@example.com", "new", "notes", "t", "t"),
        columns=_LEAD_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    lead = store.get_lead("acme", "jordan@example.com")

    assert lead["contact"] == "jordan@example.com"
    assert fake.captured["params"] == ["acme", "jordan@example.com"]


def test_get_lead_returns_none_for_no_match(monkeypatch):
    fake = _FakeConnection(row=None, columns=_LEAD_COLUMNS)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    assert store.get_lead("acme", "nobody@example.com") is None


def test_add_followup_returns_none_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", lambda tenant, contact: None)

    followup_id = store.add_followup("acme", "nobody@example.com", "2099-01-01", "nudge", "rep-1")

    assert followup_id is None


def test_add_followup_scopes_to_tenant_and_the_leads_id(monkeypatch):
    monkeypatch.setattr(store, "get_lead", lambda tenant, contact: {"id": 5})
    fake = _FakeConnection(row=(9,))
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    followup_id = store.add_followup("acme", "jordan@example.com", "2099-01-01", "nudge", "rep-1")

    assert followup_id == 9
    assert fake.captured["params"][:2] == ["acme", 5]


def test_due_followups_scopes_to_tenant_and_pending_status(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "jordan@example.com", "Jordan")],
        columns=("id", "due_at", "note", "contact", "lead_name"),
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    due = store.due_followups("acme", "2099-01-02")

    assert due[0]["contact"] == "jordan@example.com"
    assert fake.captured["params"][0] == "acme"


def test_lead_history_returns_none_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", lambda tenant, contact: None)

    assert store.lead_history("acme", "nobody@example.com") is None


def test_lead_history_includes_followups(monkeypatch):
    monkeypatch.setattr(
        store, "get_lead", lambda tenant, contact: {"id": 5, "contact": contact, "name": "Jordan"}
    )
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "pending")], columns=("id", "due_at", "note", "status")
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    history = store.lead_history("acme", "jordan@example.com")

    assert history["name"] == "Jordan"
    assert len(history["followups"]) == 1
    assert fake.captured["params"] == ["acme", 5]


_PENDING_FOLLOWUP_COLUMNS = ("id", "due_at", "note", "contact", "lead_name")


def test_list_pending_followups_scopes_to_tenant_and_pending_status(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "jordan@example.com", "Jordan")],
        columns=_PENDING_FOLLOWUP_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    followups = store.list_pending_followups("acme")

    assert followups[0]["lead_name"] == "Jordan"
    assert fake.captured["params"] == ["acme"]
    assert "status = 'pending'" in fake.captured["sql"]


def test_list_pending_followups_filters_by_contact_when_given(monkeypatch):
    fake = _FakeConnection(rows=[], columns=_PENDING_FOLLOWUP_COLUMNS)
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    store.list_pending_followups("acme", "jordan@example.com")

    assert fake.captured["params"] == ["acme", "jordan@example.com"]
    assert "l.contact = %s" in fake.captured["sql"]


def test_mark_lead_lost_returns_false_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", lambda tenant, contact: None)

    assert store.mark_lead_lost("acme", "nobody@example.com", "unresponsive") is False


def test_mark_lead_lost_updates_status_and_cancels_pending_followups(monkeypatch):
    monkeypatch.setattr(store, "get_lead", lambda tenant, contact: {"id": 5})
    fake = _FakeConnection()
    executed = []
    original_execute = fake.execute

    def _record_execute(sql, params):
        executed.append((sql, list(params)))
        return original_execute(sql, params)

    fake.execute = _record_execute
    monkeypatch.setattr(store, "get_connection", lambda: fake)

    result = store.mark_lead_lost("acme", "jordan@example.com", "went with a competitor")

    assert result is True
    assert len(executed) == 2
    lead_sql, lead_params = executed[0]
    assert "status = 'lost'" in lead_sql
    assert lead_params == ["went with a competitor", "acme", "jordan@example.com"]
    followup_sql, followup_params = executed[1]
    assert "crm_followups" in followup_sql
    assert followup_params == ["acme", 5]
