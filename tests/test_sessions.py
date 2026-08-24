"""Tests for app/sessions.py — the session directory backing item #9's
web UI switcher. Same "assert the query text/params, not a live query
result" approach tests/test_sql_store.py already uses, extended with
fetchone() (session_belongs_to's single-row lookup) alongside
fetchall() (list_sessions).
"""
from app import sessions

TEST_CTX = {"tenant": "acme", "principal": "p1", "claims": {}}


class _FakeCursor:
    def __init__(self, rows, columns, one=None):
        self._rows = rows
        self._one = one
        self.description = [type("Col", (), {"name": c}) for c in columns]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _FakeConnection:
    """`with get_connection() as conn: conn.execute(sql, params)` —
    mirrors psycopg3's Connection (itself a context manager exposing
    `.execute()` directly)."""

    def __init__(self, rows=(), columns=("thread_id", "title", "created_at", "last_active_at"), one=None):
        self.captured = {}
        self._rows = rows
        self._columns = columns
        self._one = one

    def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._rows, self._columns, self._one)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestUpsertSession:
    def test_writes_thread_tenant_principal_and_title(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.upsert_session(TEST_CTX, "t1", "What is the refund policy?")

        assert "INSERT INTO chat_sessions" in fake.captured["sql"]
        assert "ON CONFLICT (thread_id)" in fake.captured["sql"]
        assert fake.captured["params"] == ["t1", "acme", "p1", "What is the refund policy?"]

    def test_on_conflict_never_touches_title_only_last_active_at(self, monkeypatch):
        """The whole point: a session's displayed title stays whatever its
        FIRST message was, later turns only refresh last_active_at."""
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.upsert_session(TEST_CTX, "t1", "second turn's text")

        sql = fake.captured["sql"]
        conflict_clause = sql.split("ON CONFLICT (thread_id)")[1]
        assert "title" not in conflict_clause
        assert "last_active_at" in conflict_clause

    def test_long_title_is_truncated_with_an_ellipsis(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)
        long_text = "x" * 200

        sessions.upsert_session(TEST_CTX, "t1", long_text)

        title = fake.captured["params"][3]
        assert len(title) == sessions.TITLE_MAX_CHARS + 1  # +1 for the trailing "…"
        assert title.endswith("…")

    def test_blank_title_falls_back_to_a_default(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.upsert_session(TEST_CTX, "t1", "   ")

        assert fake.captured["params"][3] == "New conversation"

    def test_invalid_ctx_never_queries(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.upsert_session(None, "t1", "hello")
        sessions.upsert_session({"tenant": "", "principal": "", "claims": {}}, "t1", "hello")

        assert fake.captured == {}

    def test_missing_thread_id_never_queries(self, monkeypatch):
        fake = _FakeConnection()
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.upsert_session(TEST_CTX, "", "hello")

        assert fake.captured == {}

    def test_a_db_failure_never_raises(self, monkeypatch):
        class _RaisingConnection:
            def __enter__(self):
                raise RuntimeError("connection refused")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(sessions, "get_connection", lambda: _RaisingConnection())

        sessions.upsert_session(TEST_CTX, "t1", "hello")  # must not raise


class TestListSessions:
    def test_scopes_to_tenant_and_principal(self, monkeypatch):
        fake = _FakeConnection(rows=[])
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.list_sessions(TEST_CTX)

        assert "WHERE tenant = %s AND principal = %s" in fake.captured["sql"]
        assert fake.captured["params"] == ["acme", "p1"]

    def test_orders_most_recently_active_first(self, monkeypatch):
        fake = _FakeConnection(rows=[])
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        sessions.list_sessions(TEST_CTX)

        assert "ORDER BY last_active_at DESC" in fake.captured["sql"]

    def test_returns_dicts_not_raw_tuples(self, monkeypatch):
        fake = _FakeConnection(
            rows=[("t1", "Refund question", "2026-01-01", "2026-01-02")],
        )
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        result = sessions.list_sessions(TEST_CTX)

        assert result == [
            {
                "thread_id": "t1",
                "title": "Refund question",
                "created_at": "2026-01-01",
                "last_active_at": "2026-01-02",
            }
        ]

    def test_invalid_ctx_returns_empty_without_querying(self, monkeypatch):
        fake = _FakeConnection(rows=[("t1", "x", "y", "z")])
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        assert sessions.list_sessions(None) == []
        assert fake.captured == {}


class TestSessionBelongsTo:
    def test_true_when_a_matching_row_exists(self, monkeypatch):
        fake = _FakeConnection(one=(1,))
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        assert sessions.session_belongs_to(TEST_CTX, "t1") is True
        assert fake.captured["params"] == ["t1", "acme", "p1"]

    def test_false_when_no_matching_row(self, monkeypatch):
        fake = _FakeConnection(one=None)
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        assert sessions.session_belongs_to(TEST_CTX, "t1") is False

    def test_false_for_invalid_ctx_without_querying(self, monkeypatch):
        fake = _FakeConnection(one=(1,))
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        assert sessions.session_belongs_to(None, "t1") is False
        assert fake.captured == {}

    def test_a_different_tenant_or_principal_cannot_claim_ownership(self, monkeypatch):
        """This test doesn't prove the SQL itself is correctly scoped (a
        live-DB integration concern) — it proves the query is ALWAYS
        parameterized with ctx's own tenant/principal, never anything
        else, so a caller can't widen the check by any means available
        through this function's own interface."""
        fake = _FakeConnection(one=None)
        monkeypatch.setattr(sessions, "get_connection", lambda: fake)

        other_ctx = {"tenant": "other-co", "principal": "p9", "claims": {}}
        sessions.session_belongs_to(other_ctx, "t1")

        assert fake.captured["params"] == ["t1", "other-co", "p9"]
