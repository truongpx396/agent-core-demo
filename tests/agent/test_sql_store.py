"""Tests for app/agent/sql_store.py's query construction — the one fixed,
parameterized SQL boundary query_employees is scoped to (GRAPH_PATTERNS.md
pattern 21: fixed tools, never LLM-generated SQL).

No live Postgres here: get_connection() is monkeypatched to a fake
async-context-manager connection that records the SQL text/params it was
called with — the same "assert the server-side predicate, not a live query
result" approach test_tools.py already uses for Qdrant's tenant_filter,
applied to SQL instead of a Qdrant Filter object.

`query_employees`/`get_connection` are `async def`/`@asynccontextmanager`
now (a real `AsyncConnectionPool`, see that module's own docstring), so
every call below runs through `asyncio.run(...)`, this repo's established
pattern for exercising async code from a plain `def test_...`.
"""
import asyncio
from contextlib import asynccontextmanager

from app.agent import sql_store


class _FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [type("Col", (), {"name": c}) for c in columns]

    async def fetchall(self):
        return self._rows


class _FakeConnection:
    """`async with get_connection() as conn: await conn.execute(sql, params)`
    — mirrors psycopg3's AsyncConnection, which exposes `.execute()`
    directly (no separate `.cursor()` call needed)."""

    def __init__(self, rows=(), columns=("name", "department", "title", "hired_on")):
        self.captured = {}
        self._rows = rows
        self._columns = columns

    async def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._rows, self._columns)


def _fake_get_connection(fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    return get_connection


async def test_always_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection(fake))

    await sql_store.query_employees("ecorp")

    assert "tenant = %s" in fake.captured["sql"]
    assert fake.captured["params"] == ["ecorp"]


async def test_department_and_name_filters_are_anded_onto_tenant_never_replacing_it(
    monkeypatch,
):
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection(fake))

    await sql_store.query_employees("ecorp", department="Engineering", name_contains="ana")

    sql = fake.captured["sql"]
    assert "tenant = %s" in sql
    assert "department = %s" in sql
    assert "name ILIKE %s" in sql
    assert " AND " in sql
    assert fake.captured["params"] == ["ecorp", "Engineering", "%ana%"]


async def test_two_different_tenants_get_different_params(monkeypatch):
    """The relational-store counterpart to test_tools.py's
    test_two_different_tenants_get_different_filters — same isolation
    property, proven against the SQL params instead of a Qdrant Filter."""
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection(fake))

    await sql_store.query_employees("ecorp")
    ecorp_params = fake.captured["params"]
    await sql_store.query_employees("other-co")
    other_params = fake.captured["params"]

    assert ecorp_params != other_params
    assert ecorp_params == ["ecorp"]
    assert other_params == ["other-co"]


async def test_returns_dicts_not_raw_tuples(monkeypatch):
    fake = _FakeConnection(
        rows=[("Priya Nair", "Engineering", "Staff Engineer", "2021-03-01")],
    )
    monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection(fake))

    result = await sql_store.query_employees("ecorp")

    assert result == [
        {
            "name": "Priya Nair",
            "department": "Engineering",
            "title": "Staff Engineer",
            "hired_on": "2021-03-01",
        }
    ]


class _FakeConnCM:
    """Stands in for `AsyncConnectionPool.connection()`'s own return value
    — an async context manager yielding one checked-out connection."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class TestConnectionPool:
    """The pool itself (GRAPH_PATTERNS.md pattern 31) — a lazy singleton,
    reset before/after each test so these don't leak a real pool (with
    live background tasks) into other test files."""

    def setup_method(self):
        sql_store._pool = None

    def teardown_method(self):
        # Plain sync, not async def: pytest's own xunit-style setup_method/
        # teardown_method hooks are called directly (`func()`), never
        # awaited, even under pytest-asyncio's asyncio_mode="auto" — verified
        # empirically (an `async def teardown_method` here produced a real
        # "coroutine was never awaited" warning and silently never ran).
        # asyncio.run(...) bridges it, same as every other sync-context
        # caller of an async function in this repo.
        asyncio.run(sql_store.close_pool())

    async def test_close_pool_is_a_noop_when_never_opened(self):
        assert sql_store._pool is None
        await sql_store.close_pool()  # must not raise
        assert sql_store._pool is None

    async def test_get_connection_lazily_opens_a_singleton_pool(self, monkeypatch):
        created = []

        class _FakePool:
            def __init__(self, *a, **kw):
                created.append((a, kw))

            async def open(self, wait=True):
                pass

            def connection(self):
                return _FakeConnCM("a-connection")

            async def close(self):
                pass

        monkeypatch.setattr(sql_store, "AsyncConnectionPool", _FakePool)

        async def _get_value():
            async with sql_store.get_connection() as conn:
                return conn

        first = await _get_value()
        second = await _get_value()

        assert len(created) == 1  # the pool itself, constructed once
        assert first == second == "a-connection"

    async def test_close_pool_clears_the_singleton_so_a_later_call_reopens(self, monkeypatch):
        created = []

        class _FakePool:
            def __init__(self, *a, **kw):
                created.append(1)

            async def open(self, wait=True):
                pass

            def connection(self):
                return _FakeConnCM(None)

            async def close(self):
                pass

        monkeypatch.setattr(sql_store, "AsyncConnectionPool", _FakePool)

        async def _touch():
            async with sql_store.get_connection():
                pass

        await _touch()
        await sql_store.close_pool()
        assert sql_store._pool is None
        await _touch()

        assert len(created) == 2
