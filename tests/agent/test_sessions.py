"""Tests for app/agent/sessions.py — the session directory backing item #9's
web UI switcher. Same "assert the query text/params, not a live query
result" approach tests/agent/test_sql_store.py already uses, extended with
fetchone() (session_belongs_to's single-row lookup) alongside
fetchall() (list_sessions).

`upsert_session`/`list_sessions`/`session_belongs_to`/`get_connection` are
all `async def`/`@asynccontextmanager` now (a real `AsyncConnectionPool`,
see app/agent/sql_store.py's own docstring), so every call below runs
through `asyncio.run(...)`, this repo's established pattern for exercising
async code from a plain `def test_...`.
"""
from contextlib import asynccontextmanager

from app.agent import sessions

TEST_CTX = {"tenant": "ecorp", "principal": "p1", "claims": {}}


class _FakeCursor:
    def __init__(self, rows, columns, one=None):
        self._rows = rows
        self._one = one
        self.description = [type("Col", (), {"name": c}) for c in columns]

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._one


class _FakeConnection:
    """`async with get_connection() as conn: await conn.execute(sql, params)`
    — mirrors psycopg3's AsyncConnection, which exposes `.execute()`
    directly (no separate `.cursor()` call needed)."""

    def __init__(self, rows=(), columns=("thread_id", "title", "created_at", "last_active_at"), one=None):
        self.captured = {}
        self._rows = rows
        self._columns = columns
        self._one = one

    async def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._rows, self._columns, self._one)


def _fake_get_connection(fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    return get_connection


class TestUpsertSession:
    async def test_writes_thread_tenant_principal_and_title(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(TEST_CTX, "t1", "What is the refund policy?")

        assert "INSERT INTO chat_sessions" in fake.captured["sql"]
        assert "ON CONFLICT (thread_id)" in fake.captured["sql"]
        assert fake.captured["params"] == ["t1", "ecorp", "p1", "What is the refund policy?", "ecorp"]

    async def test_writes_a_non_default_domain_too(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(TEST_CTX, "t1", "where is my order?", domain="support")

        assert fake.captured["params"][-1] == "support"

    async def test_on_conflict_never_touches_title_or_domain_only_last_active_at(self, monkeypatch):
        """The whole point: a session's displayed title AND domain stay
        whatever its FIRST turn actually was, later turns only refresh
        last_active_at (see this module's own docstring on why a domain
        can't be silently reassigned by a later turn)."""
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(TEST_CTX, "t1", "second turn's text")

        sql = fake.captured["sql"]
        conflict_clause = sql.split("ON CONFLICT (thread_id)")[1]
        assert "title" not in conflict_clause
        assert "domain" not in conflict_clause
        assert "last_active_at" in conflict_clause

    async def test_long_title_is_truncated_with_an_ellipsis(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))
        long_text = "x" * 200

        await sessions.upsert_session(TEST_CTX, "t1", long_text)

        title = fake.captured["params"][3]
        assert len(title) == sessions.TITLE_MAX_CHARS + 1  # +1 for the trailing "…"
        assert title.endswith("…")

    async def test_blank_title_falls_back_to_a_default(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(TEST_CTX, "t1", "   ")

        assert fake.captured["params"][3] == "New conversation"

    async def test_invalid_ctx_never_queries(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(None, "t1", "hello")
        await sessions.upsert_session({"tenant": "", "principal": "", "claims": {}}, "t1", "hello")

        assert fake.captured == {}

    async def test_missing_thread_id_never_queries(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.upsert_session(TEST_CTX, "", "hello")

        assert fake.captured == {}

    async def test_a_db_failure_never_raises(self, monkeypatch):
        @asynccontextmanager
        async def _raising_get_connection():
            raise RuntimeError("connection refused")
            yield  # pragma: no cover - unreachable, makes this a generator

        monkeypatch.setattr(sessions, "get_connection", _raising_get_connection)

        await sessions.upsert_session(TEST_CTX, "t1", "hello")  # must not raise


class TestListSessions:
    async def test_scopes_to_tenant_principal_and_domain(self, monkeypatch):
        fake = _FakeConnection(rows=[])
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.list_sessions(TEST_CTX)

        assert "WHERE tenant = %s AND principal = %s AND domain = %s" in fake.captured["sql"]
        assert fake.captured["params"] == ["ecorp", "p1", "ecorp"]

    async def test_a_different_domain_is_a_different_scope(self, monkeypatch):
        fake = _FakeConnection(rows=[])
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.list_sessions(TEST_CTX, domain="support")

        assert fake.captured["params"] == ["ecorp", "p1", "support"]

    async def test_orders_most_recently_active_first(self, monkeypatch):
        fake = _FakeConnection(rows=[])
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.list_sessions(TEST_CTX)

        assert "ORDER BY last_active_at DESC" in fake.captured["sql"]

    async def test_returns_dicts_not_raw_tuples(self, monkeypatch):
        fake = _FakeConnection(
            rows=[("t1", "Refund question", "2026-01-01", "2026-01-02")],
        )
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        result = await sessions.list_sessions(TEST_CTX)

        assert result == [
            {
                "thread_id": "t1",
                "title": "Refund question",
                "created_at": "2026-01-01",
                "last_active_at": "2026-01-02",
            }
        ]

    async def test_invalid_ctx_returns_empty_without_querying(self, monkeypatch):
        fake = _FakeConnection(rows=[("t1", "x", "y", "z")])
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        assert await sessions.list_sessions(None) == []
        assert fake.captured == {}


class TestSessionBelongsTo:
    async def test_true_when_a_matching_row_exists(self, monkeypatch):
        fake = _FakeConnection(one=(1,))
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        assert await sessions.session_belongs_to(TEST_CTX, "t1") is True
        assert fake.captured["params"] == ["t1", "ecorp", "p1", "ecorp"]

    async def test_domain_is_always_passed_as_a_query_param_not_checked_after_the_fact(self, monkeypatch):
        """Same "prove the query is ALWAYS parameterized" spirit as
        test_a_different_tenant_or_principal_cannot_claim_ownership below —
        a live-DB integration concern, not provable against this fake
        (which returns its canned `one` regardless of params). The real
        enforcement is Postgres actually filtering `AND domain = %s`; what
        this proves is that a caller can't get a "sales" thread checked
        against "support" by any means this function's interface allows —
        exactly the same gap this pattern closed for tenant/principal
        already."""
        fake = _FakeConnection(one=(1,))
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        await sessions.session_belongs_to(TEST_CTX, "t1", domain="sales")

        assert fake.captured["params"] == ["t1", "ecorp", "p1", "sales"]

    async def test_false_when_no_matching_row(self, monkeypatch):
        fake = _FakeConnection(one=None)
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        assert await sessions.session_belongs_to(TEST_CTX, "t1") is False

    async def test_false_for_invalid_ctx_without_querying(self, monkeypatch):
        fake = _FakeConnection(one=(1,))
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        assert await sessions.session_belongs_to(None, "t1") is False
        assert fake.captured == {}

    async def test_a_different_tenant_or_principal_cannot_claim_ownership(self, monkeypatch):
        """This test doesn't prove the SQL itself is correctly scoped (a
        live-DB integration concern) — it proves the query is ALWAYS
        parameterized with ctx's own tenant/principal, never anything
        else, so a caller can't widen the check by any means available
        through this function's own interface."""
        fake = _FakeConnection(one=None)
        monkeypatch.setattr(sessions, "get_connection", _fake_get_connection(fake))

        other_ctx = {"tenant": "other-co", "principal": "p9", "claims": {}}
        await sessions.session_belongs_to(other_ctx, "t1")

        assert fake.captured["params"] == ["t1", "other-co", "p9", "ecorp"]
