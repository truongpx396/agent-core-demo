"""Tests for app/domains/sales/store.py's query construction — same
"assert the SQL text/params, no live Postgres" approach as
tests/agent/test_sql_store.py / tests/domains/support/test_store.py.

`add_followup`/`lead_history` each issue TWO queries — first `get_lead`,
then their own — so those two are tested with `get_lead` itself
monkeypatched (a real, public function this module already exposes)
rather than juggling a multi-call fake connection.

Every function here is `async def` now (a real `AsyncConnectionPool`, see
app/agent/sql_store.py's own docstring), so every call below runs through
`asyncio.run(...)`, this repo's established pattern for exercising async
code from a plain `def test_...`.
"""
from contextlib import asynccontextmanager

from app.domains.sales import store


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


def _fake_get_lead(result):
    async def get_lead(tenant, contact):
        return result

    return get_lead


_LEAD_COLUMNS = ("id", "tenant", "name", "contact", "status", "created_at", "updated_at", "notes")


class _SequencedConnection:
    """Returns one fixed row per call, in order — find_or_create_lead/
    append_lead_note/mark_lead_lost each issue more than one statement
    now that notes live in their own table, unlike the single-query
    functions `_FakeConnection` above already covers."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.captured_sqls = []
        self.captured_params = []

    async def execute(self, sql, params):
        self.captured_sqls.append(sql)
        self.captured_params.append(list(params))
        return _FakeCursor(self._rows.pop(0) if self._rows else None)


async def test_find_or_create_lead_always_scopes_to_tenant(monkeypatch):
    fake = _SequencedConnection(rows=[(1,), None])  # lead upsert, then the note insert
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    lead_id = await store.find_or_create_lead("ecorp", "Jordan", "jordan@example.com", "asked about pricing")

    assert lead_id == 1
    assert fake.captured_params[0] == ["ecorp", "Jordan", "jordan@example.com"]
    assert "crm_leads" in fake.captured_sqls[0]
    assert "notes" not in fake.captured_sqls[0]  # the upsert itself no longer touches notes at all


async def test_find_or_create_lead_logs_the_note_as_its_own_row(monkeypatch):
    fake = _SequencedConnection(rows=[(1,), None])
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.find_or_create_lead(
        "ecorp", "Jordan", "jordan@example.com", "asked about pricing", tool_call_id="call-1"
    )

    assert "INSERT INTO crm_lead_notes" in fake.captured_sqls[1]
    assert "ON CONFLICT (tool_call_id) DO NOTHING" in fake.captured_sqls[1]
    assert fake.captured_params[1] == ["ecorp", 1, "asked about pricing", "call-1"]


async def test_get_lead_scopes_to_tenant_and_contact(monkeypatch):
    fake = _FakeConnection(
        row=(1, "ecorp", "Jordan", "jordan@example.com", "new", "t", "t", "asked about pricing"),
        columns=_LEAD_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    lead = await store.get_lead("ecorp", "jordan@example.com")

    assert lead["contact"] == "jordan@example.com"
    # notes now comes back from STRING_AGG over crm_lead_notes (postgres-init/
    # 15-append-notes-as-rows.sql), not a stored column — proves the dict key
    # still comes through unchanged either way.
    assert lead["notes"] == "asked about pricing"
    assert fake.captured["params"] == ["ecorp", "jordan@example.com"]
    assert "crm_lead_notes" in fake.captured["sql"]
    assert "GROUP BY" in fake.captured["sql"]


async def test_get_lead_returns_none_for_no_match(monkeypatch):
    fake = _FakeConnection(row=None, columns=_LEAD_COLUMNS)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.get_lead("ecorp", "nobody@example.com") is None


async def test_append_lead_note_returns_false_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead(None))

    assert await store.append_lead_note("ecorp", "nobody@example.com", "did research") is False


async def test_append_lead_note_inserts_a_note_row_and_bumps_the_leads_updated_at(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _SequencedConnection(rows=[None, None])
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    result = await store.append_lead_note("ecorp", "jordan@example.com", "did research")

    assert result is True
    assert "INSERT INTO crm_lead_notes" in fake.captured_sqls[0]
    assert "ON CONFLICT (tool_call_id) DO NOTHING" in fake.captured_sqls[0]
    assert fake.captured_params[0] == ["ecorp", 5, "did research", None]
    assert "crm_leads" in fake.captured_sqls[1]
    assert "updated_at" in fake.captured_sqls[1]


async def test_append_lead_note_passes_the_tool_call_id_through(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _SequencedConnection(rows=[None, None])
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.append_lead_note("ecorp", "jordan@example.com", "did research", tool_call_id="call-1")

    assert fake.captured_params[0] == ["ecorp", 5, "did research", "call-1"]


async def test_add_followup_returns_none_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead(None))

    followup_id = await store.add_followup("ecorp", "nobody@example.com", "2099-01-01", "nudge", "rep-1")

    assert followup_id is None


async def test_add_followup_scopes_to_tenant_and_the_leads_id(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _FakeConnection(row=(9,))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    followup_id = await store.add_followup("ecorp", "jordan@example.com", "2099-01-01", "nudge", "rep-1")

    assert followup_id == 9
    assert fake.captured["params"][:2] == ["ecorp", 5]
    assert fake.captured["params"][-1] is None  # tool_call_id defaults to None


async def test_add_followup_passes_the_tool_call_id_through(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _FakeConnection(row=(9,))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.add_followup(
        "ecorp", "jordan@example.com", "2099-01-01", "nudge", "rep-1", tool_call_id="call-1"
    )

    assert fake.captured["params"][-1] == "call-1"


async def test_add_followup_a_repeated_tool_call_id_returns_the_original_row_not_a_duplicate(
    monkeypatch,
):
    """ON CONFLICT (tool_call_id) DO NOTHING firing (no row RETURNING'd)
    falls back to reading back the existing follow-up's id — proves the
    exactly-once-at-the-row guarantee postgres-init/14-tool-call-id-columns.sql
    exists for, without a live Postgres."""
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))

    class _ConflictThenSelect:
        def __init__(self):
            self.captured_sqls = []

        async def execute(self, sql, params):
            self.captured_sqls.append(sql)
            if sql.strip().startswith("INSERT"):
                return _FakeCursor(None)
            return _FakeCursor((11,))

    fake = _ConflictThenSelect()
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    followup_id = await store.add_followup(
        "ecorp", "jordan@example.com", "2099-01-01", "nudge", "rep-1", tool_call_id="call-1"
    )

    assert followup_id == 11
    assert len(fake.captured_sqls) == 2


async def test_due_followups_scopes_to_tenant_and_pending_status(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "jordan@example.com", "Jordan")],
        columns=("id", "due_at", "note", "contact", "lead_name"),
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    due = await store.due_followups("ecorp", "2099-01-02")

    assert due[0]["contact"] == "jordan@example.com"
    assert fake.captured["params"][0] == "ecorp"


async def test_lead_history_returns_none_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead(None))

    assert await store.lead_history("ecorp", "nobody@example.com") is None


async def test_lead_history_includes_followups(monkeypatch):
    async def get_lead(tenant, contact):
        return {"id": 5, "contact": contact, "name": "Jordan"}

    monkeypatch.setattr(store, "get_lead", get_lead)
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "pending")], columns=("id", "due_at", "note", "status")
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    history = await store.lead_history("ecorp", "jordan@example.com")

    assert history["name"] == "Jordan"
    assert len(history["followups"]) == 1
    assert fake.captured["params"] == ["ecorp", 5]


_PENDING_FOLLOWUP_COLUMNS = ("id", "due_at", "note", "contact", "lead_name")


async def test_list_pending_followups_scopes_to_tenant_and_pending_status(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "2099-01-01", "nudge", "jordan@example.com", "Jordan")],
        columns=_PENDING_FOLLOWUP_COLUMNS,
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    followups = await store.list_pending_followups("ecorp")

    assert followups[0]["lead_name"] == "Jordan"
    assert fake.captured["params"] == ["ecorp"]
    assert "status = 'pending'" in fake.captured["sql"]


async def test_list_pending_followups_filters_by_contact_when_given(monkeypatch):
    fake = _FakeConnection(rows=[], columns=_PENDING_FOLLOWUP_COLUMNS)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.list_pending_followups("ecorp", "jordan@example.com")

    assert fake.captured["params"] == ["ecorp", "jordan@example.com"]
    assert "l.contact = %s" in fake.captured["sql"]


async def test_mark_lead_lost_returns_false_when_no_lead_exists(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead(None))

    assert await store.mark_lead_lost("ecorp", "nobody@example.com", "unresponsive") is False


async def test_mark_lead_lost_updates_status_logs_a_note_and_cancels_pending_followups(monkeypatch):
    """The reason is now its own crm_lead_notes row (postgres-init/
    15-append-notes-as-rows.sql), not appended inline onto the same
    UPDATE that sets status — three statements, not two."""
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _FakeConnection()
    executed = []
    original_execute = fake.execute

    async def _record_execute(sql, params):
        executed.append((sql, list(params)))
        return await original_execute(sql, params)

    fake.execute = _record_execute
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    result = await store.mark_lead_lost("ecorp", "jordan@example.com", "went with a competitor")

    assert result is True
    assert len(executed) == 3
    status_sql, status_params = executed[0]
    assert "status = 'lost'" in status_sql
    assert "notes" not in status_sql  # no longer touches notes directly at all
    assert status_params == ["ecorp", "jordan@example.com"]
    note_sql, note_params = executed[1]
    assert "INSERT INTO crm_lead_notes" in note_sql
    assert "ON CONFLICT (tool_call_id) DO NOTHING" in note_sql
    assert note_params == ["ecorp", 5, "went with a competitor", None]
    followup_sql, followup_params = executed[2]
    assert "crm_followups" in followup_sql
    assert followup_params == ["ecorp", 5]


async def test_mark_lead_lost_passes_the_tool_call_id_through_to_the_note(monkeypatch):
    monkeypatch.setattr(store, "get_lead", _fake_get_lead({"id": 5}))
    fake = _FakeConnection()
    executed = []
    original_execute = fake.execute

    async def _record_execute(sql, params):
        executed.append(list(params))
        return await original_execute(sql, params)

    fake.execute = _record_execute
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.mark_lead_lost(
        "ecorp", "jordan@example.com", "went with a competitor", tool_call_id="call-1"
    )

    assert executed[1] == ["ecorp", 5, "went with a competitor", "call-1"]
