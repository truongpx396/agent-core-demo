"""Tests for app/domains/support/store.py's query construction — same
"assert the SQL text/params, no live Postgres" approach as
tests/agent/test_sql_store.py.

Every function here is `async def` now (a real `AsyncConnectionPool`, see
app/agent/sql_store.py's own docstring), so every call below runs through
`asyncio.run(...)`, this repo's established pattern for exercising async
code from a plain `def test_...`.
"""
from contextlib import asynccontextmanager

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
                    "status", "escalation_reason", "created_at", "updated_at", "notes",
                )
            )
        ]

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, row=None, rows=None, rowcount=0, columns=None):
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


async def test_create_ticket_always_scopes_to_tenant_and_returns_the_new_id(monkeypatch):
    fake = _FakeConnection(row=(42,))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    ticket_id = await store.create_ticket("ecorp", "alice", "Login broken", "Can't log in", "high")

    assert ticket_id == 42
    assert "tenant" in fake.captured["sql"]
    assert fake.captured["params"][0] == "ecorp"
    assert fake.captured["params"][-1] is None  # tool_call_id defaults to None


async def test_create_ticket_passes_the_tool_call_id_through(monkeypatch):
    fake = _FakeConnection(row=(42,))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.create_ticket(
        "ecorp", "alice", "Login broken", "Can't log in", "high", tool_call_id="call-1"
    )

    assert fake.captured["params"][-1] == "call-1"


async def test_create_ticket_a_repeated_tool_call_id_returns_the_original_row_not_a_duplicate(
    monkeypatch,
):
    """ON CONFLICT (tool_call_id) DO NOTHING firing (no row RETURNING'd)
    falls back to reading back the existing ticket's id — proves the
    exactly-once-at-the-row guarantee postgres-init/14-tool-call-id-columns.sql
    exists for, without a live Postgres."""

    class _ConflictThenSelect:
        def __init__(self):
            self.captured_sqls = []

        async def execute(self, sql, params):
            self.captured_sqls.append(sql)
            if sql.strip().startswith("INSERT"):
                return _FakeCursor(None)
            return _FakeCursor((9,))

    fake = _ConflictThenSelect()
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    ticket_id = await store.create_ticket(
        "ecorp", "alice", "Login broken", "Can't log in", "high", tool_call_id="call-1"
    )

    assert ticket_id == 9
    assert len(fake.captured_sqls) == 2


async def test_get_ticket_scopes_to_tenant_and_id(monkeypatch):
    fake = _FakeConnection(
        row=(1, "ecorp", "alice", "s", "d", "normal", "open", None, "t", "t", "first\nsecond")
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    ticket = await store.get_ticket("ecorp", 1)

    assert ticket["id"] == 1
    # notes now comes back from STRING_AGG over support_ticket_comments
    # (postgres-init/15-append-notes-as-rows.sql), not a stored column —
    # this proves the dict key still comes through unchanged either way.
    assert ticket["notes"] == "first\nsecond"
    assert fake.captured["params"] == ["ecorp", 1]
    assert "support_ticket_comments" in fake.captured["sql"]
    assert "GROUP BY" in fake.captured["sql"]


async def test_get_ticket_returns_none_for_no_match(monkeypatch):
    fake = _FakeConnection(row=None)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.get_ticket("ecorp", 999) is None


async def test_escalate_ticket_returns_false_when_no_row_updated(monkeypatch):
    fake = _FakeConnection(rowcount=0)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.escalate_ticket("ecorp", 999, "reason") is False


async def test_escalate_ticket_returns_true_and_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection(rowcount=1)
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.escalate_ticket("ecorp", 1, "billing issue") is True
    assert fake.captured["params"] == ["billing issue", "ecorp", 1]


async def test_list_tickets_for_requester_scopes_to_tenant_and_requester(monkeypatch):
    fake = _FakeConnection(
        rows=[(1, "Login broken", "high", "open", "t")],
        columns=("id", "subject", "priority", "status", "created_at"),
    )
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    tickets = await store.list_tickets_for_requester("ecorp", "alice")

    assert tickets[0]["subject"] == "Login broken"
    assert fake.captured["params"][:2] == ["ecorp", "alice"]


async def test_list_tickets_for_requester_respects_the_limit_param(monkeypatch):
    fake = _FakeConnection(rows=[], columns=("id", "subject", "priority", "status", "created_at"))
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.list_tickets_for_requester("ecorp", "alice", limit=3)

    assert fake.captured["params"] == ["ecorp", "alice", 3]


class _SequencedConnection:
    """Returns one fixed row per call, in order — add_comment issues up to
    three statements (ticket-exists check, insert, updated_at bump), each
    needing its own canned response, unlike the single-query functions
    `_FakeConnection` above already covers."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.captured_sqls = []
        self.captured_params = []

    async def execute(self, sql, params):
        self.captured_sqls.append(sql)
        self.captured_params.append(list(params))
        return _FakeCursor(self._rows.pop(0) if self._rows else None)


async def test_add_comment_returns_false_when_no_ticket_exists(monkeypatch):
    fake = _SequencedConnection(rows=[None])  # the existence check finds nothing
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.add_comment("ecorp", 999, "still broken") is False
    assert len(fake.captured_sqls) == 1  # never reached the INSERT


async def test_add_comment_inserts_a_comment_row_scoped_to_tenant(monkeypatch):
    fake = _SequencedConnection(rows=[(1,), None, None])  # exists, insert, updated_at bump
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    assert await store.add_comment("ecorp", 1, "still broken") is True
    insert_sql, insert_params = fake.captured_sqls[1], fake.captured_params[1]
    assert "INSERT INTO support_ticket_comments" in insert_sql
    assert "ON CONFLICT (tool_call_id) DO NOTHING" in insert_sql
    assert insert_params == ["ecorp", 1, "still broken", None]


async def test_add_comment_passes_the_tool_call_id_through(monkeypatch):
    fake = _SequencedConnection(rows=[(1,), None, None])
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    await store.add_comment("ecorp", 1, "still broken", tool_call_id="call-1")

    assert fake.captured_params[1] == ["ecorp", 1, "still broken", "call-1"]


async def test_add_comment_a_repeated_tool_call_id_is_a_no_op_not_a_duplicate(monkeypatch):
    """The actual fix: ON CONFLICT DO NOTHING means a genuine replay under
    the same tool_call_id never produces a second comment row — unlike
    the old column-append version, which doubled the text every time."""
    fake = _SequencedConnection(rows=[(1,), None, None])
    monkeypatch.setattr(store, "get_connection", _fake_get_connection(fake))

    result = await store.add_comment("ecorp", 1, "still broken", tool_call_id="call-1")

    assert result is True  # still reports success — the comment IS recorded, just not twice
    assert len(fake.captured_sqls) == 3  # exists-check, insert (no-ops), updated_at bump — never a second insert
