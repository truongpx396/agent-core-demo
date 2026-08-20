"""Tests for app/sql_store.py's query construction — the one fixed,
parameterized SQL boundary query_employees is scoped to (GRAPH_PATTERNS.md
pattern 21: fixed tools, never LLM-generated SQL).

No live Postgres here: get_connection() is monkeypatched to a fake
connection that records the SQL text/params it was called with — the same
"assert the server-side predicate, not a live query result" approach
test_tools.py already uses for Qdrant's tenant_filter, applied to SQL
instead of a Qdrant Filter object.
"""
from app import sql_store


class _FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [type("Col", (), {"name": c}) for c in columns]

    def fetchall(self):
        return self._rows


class _FakeConnection:
    """`with get_connection() as conn: conn.execute(sql, params)` — mirrors
    psycopg3's Connection, which is itself a context manager and exposes
    `.execute()` directly (no separate `.cursor()` call needed)."""

    def __init__(self, rows=(), columns=("name", "department", "title", "hired_on")):
        self.captured = {}
        self._rows = rows
        self._columns = columns

    def execute(self, sql, params):
        self.captured["sql"] = sql
        self.captured["params"] = list(params)
        return _FakeCursor(self._rows, self._columns)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_always_scopes_to_tenant(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", lambda: fake)

    sql_store.query_employees("acme")

    assert "tenant = %s" in fake.captured["sql"]
    assert fake.captured["params"] == ["acme"]


def test_department_and_name_filters_are_anded_onto_tenant_never_replacing_it(
    monkeypatch,
):
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", lambda: fake)

    sql_store.query_employees("acme", department="Engineering", name_contains="ana")

    sql = fake.captured["sql"]
    assert "tenant = %s" in sql
    assert "department = %s" in sql
    assert "name ILIKE %s" in sql
    assert " AND " in sql
    assert fake.captured["params"] == ["acme", "Engineering", "%ana%"]


def test_two_different_tenants_get_different_params(monkeypatch):
    """The relational-store counterpart to test_tools.py's
    test_two_different_tenants_get_different_filters — same isolation
    property, proven against the SQL params instead of a Qdrant Filter."""
    fake = _FakeConnection()
    monkeypatch.setattr(sql_store, "get_connection", lambda: fake)

    sql_store.query_employees("acme")
    acme_params = fake.captured["params"]
    sql_store.query_employees("other-co")
    other_params = fake.captured["params"]

    assert acme_params != other_params
    assert acme_params == ["acme"]
    assert other_params == ["other-co"]


def test_returns_dicts_not_raw_tuples(monkeypatch):
    fake = _FakeConnection(
        rows=[("Priya Nair", "Engineering", "Staff Engineer", "2021-03-01")],
    )
    monkeypatch.setattr(sql_store, "get_connection", lambda: fake)

    result = sql_store.query_employees("acme")

    assert result == [
        {
            "name": "Priya Nair",
            "department": "Engineering",
            "title": "Staff Engineer",
            "hired_on": "2021-03-01",
        }
    ]
