"""Tests for app/agent/tool_idempotency.py's idempotent() — the exactly-once
guard every mutating/outward tool now runs its real side effect through.

Same "no live Postgres" approach as tests/agent/test_usage_ledger.py: a
tiny in-memory fake stands in for the tool_call_dedup table, real enough to
prove idempotent()'s actual claim/cache-hit/store logic (not just record
calls) — get_connection() is called up to three times per idempotent()
invocation (claim, maybe a cached-result read, the closing store), so the
fake must share state across those calls the same way a real pooled
connection's underlying table would.
"""
from contextlib import asynccontextmanager

from app.agent import tool_idempotency
from app.core import metrics
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeDedupStore:
    """In-memory stand-in for the tool_call_dedup table."""

    def __init__(self):
        self.rows: dict[str, dict] = {}


class _FakeDedupConnection:
    def __init__(self, store: _FakeDedupStore):
        self._store = store

    async def execute(self, sql, params):
        if sql.startswith("INSERT"):
            tool_call_id, tenant, thread_id, tool_name = params
            if tool_call_id in self._store.rows:
                return _FakeCursor(None)  # ON CONFLICT DO NOTHING -> no RETURNING row
            self._store.rows[tool_call_id] = {
                "tenant": tenant,
                "thread_id": thread_id,
                "tool_name": tool_name,
                "result": None,
            }
            return _FakeCursor((tool_call_id,))
        if sql.startswith("SELECT"):
            (tool_call_id,) = params
            row = self._store.rows.get(tool_call_id)
            return _FakeCursor((row["result"],) if row is not None else None)
        if sql.startswith("UPDATE"):
            result, tool_call_id = params
            if tool_call_id in self._store.rows:
                self._store.rows[tool_call_id]["result"] = result
            return _FakeCursor(None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _fake_get_connection(store: _FakeDedupStore):
    @asynccontextmanager
    async def get_connection():
        yield _FakeDedupConnection(store)

    return get_connection


def _cfg(thread_id="t1"):
    return {"configurable": {"thread_id": thread_id}}


class TestIdempotent:
    async def test_first_call_runs_fn_and_stores_the_result(self, monkeypatch):
        store = _FakeDedupStore()
        monkeypatch.setattr(tool_idempotency, "get_connection", _fake_get_connection(store))
        calls = []

        async def fn():
            calls.append(1)
            return "did the thing"

        result = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert result == "did the thing"
        assert calls == [1]
        assert store.rows["call-1"]["result"] == "did the thing"
        assert store.rows["call-1"]["tenant"] == TEST_CTX["tenant"]
        assert store.rows["call-1"]["thread_id"] == "t1"
        assert store.rows["call-1"]["tool_name"] == "add_note"

    async def test_a_second_call_with_the_same_id_returns_the_cached_result_without_rerunning_fn(
        self, monkeypatch
    ):
        store = _FakeDedupStore()
        monkeypatch.setattr(tool_idempotency, "get_connection", _fake_get_connection(store))
        calls = []

        async def fn():
            calls.append(1)
            return "did the thing"

        first = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )
        second = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert first == second == "did the thing"
        assert calls == [1]  # fn only ran once, across both calls

    async def test_different_tool_call_ids_each_run_fn_independently(self, monkeypatch):
        store = _FakeDedupStore()
        monkeypatch.setattr(tool_idempotency, "get_connection", _fake_get_connection(store))
        calls = []

        async def fn():
            calls.append(1)
            return f"result-{len(calls)}"

        first = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )
        second = await tool_idempotency.idempotent(
            tool_call_id="call-2", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert (first, second) == ("result-1", "result-2")
        assert calls == [1, 1]

    async def test_a_still_in_flight_claim_falls_through_and_runs_fn_too(self, monkeypatch):
        """A row that exists but has result IS NULL (another caller
        claimed it and hasn't finished, or died before storing) is the one
        accepted race idempotent()'s own docstring documents — proves the
        fallback path runs fn() rather than hanging or raising."""
        store = _FakeDedupStore()
        store.rows["call-1"] = {"tenant": "x", "thread_id": "t1", "tool_name": "add_note", "result": None}
        monkeypatch.setattr(tool_idempotency, "get_connection", _fake_get_connection(store))
        calls = []

        async def fn():
            calls.append(1)
            return "ran anyway"

        result = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert result == "ran anyway"
        assert calls == [1]

    async def test_fails_open_when_the_connection_itself_raises(self, monkeypatch):
        def _broken():
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(tool_idempotency, "get_connection", _broken)
        calls = []

        async def fn():
            calls.append(1)
            return "ran unprotected"

        before = _count(metrics.agent_tool_dedup_degraded_total)

        result = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert result == "ran unprotected"
        assert calls == [1]
        assert _count(metrics.agent_tool_dedup_degraded_total) == before + 1

    async def test_fails_open_when_storing_the_result_raises_but_keeps_the_real_result(self, monkeypatch):
        """fn() already ran and produced a real answer by the time the
        closing UPDATE is attempted — a failure to CACHE that answer must
        never throw away the answer itself."""
        store = _FakeDedupStore()
        real_conn = _fake_get_connection(store)
        calls = {"n": 0}

        @asynccontextmanager
        async def flaky_get_connection():
            calls["n"] += 1
            if calls["n"] == 2:  # the closing UPDATE call
                raise ConnectionError("dropped mid-write")
            async with real_conn() as conn:
                yield conn

        monkeypatch.setattr(tool_idempotency, "get_connection", flaky_get_connection)

        async def fn():
            return "the real answer"

        before = _count(metrics.agent_tool_dedup_degraded_total)

        result = await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config=_cfg(), tool_name="add_note", fn=fn
        )

        assert result == "the real answer"
        assert _count(metrics.agent_tool_dedup_degraded_total) == before + 1

    async def test_thread_id_is_optional_when_config_has_none(self, monkeypatch):
        store = _FakeDedupStore()
        monkeypatch.setattr(tool_idempotency, "get_connection", _fake_get_connection(store))

        async def fn():
            return "ok"

        await tool_idempotency.idempotent(
            tool_call_id="call-1", ctx=TEST_CTX, config={}, tool_name="add_note", fn=fn
        )

        assert store.rows["call-1"]["thread_id"] is None
