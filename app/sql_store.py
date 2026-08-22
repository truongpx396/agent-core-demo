"""Structured-data access: fixed, parameterized queries against Postgres —
never LLM-generated SQL (GRAPH_PATTERNS.md pattern 21 — the "fixed tools,
never generated queries" note from pattern 15, finally with something to
apply it to).

`query_employees` is the ONLY query this module can run. There is no
`execute(sql: str)` escape hatch, no way to reach this module from
app/tools.py except through that one function's typed, closed parameter
set. That is the entire point: a tool that let the model construct a
WHERE clause (or worse, full SQL text) would be an injection and
exfiltration surface no per-tenant scoping could reliably bound — the
access boundary has to live in code that's reviewable and testable, not
in a string the model assembles. Every query this module issues is
parameterized (`%s` placeholders, psycopg's own escaping) AND always
includes `WHERE tenant = %s` with a value from `SecurityCtx`, never from
caller-supplied text — the same pre-filter discipline
app/qdrant_store.py's hybrid_search already applies, just against a
relational store instead of a vector one.
"""
from psycopg_pool import ConnectionPool

from app.config import APPDATA_DATABASE_URL

# One pool for the process lifetime — every caller (query_employees below,
# app/meter.py's usage_ledger reads/writes) shares it via get_connection(),
# rather than each paying a fresh TCP+auth handshake per call the way a
# bare `psycopg.connect()` per call did before. Opened lazily (on first
# get_connection() call, not at import time) so importing this module
# never implies a network dependency — matches app/qdrant_store.py's
# get_client() and app/semantic_cache.py's _get_client() doing the same
# for their own stores.
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(APPDATA_DATABASE_URL, min_size=1, max_size=10, open=True)
    return _pool


def get_connection():
    """A pooled connection, checked out for the caller's `with` block and
    returned to the pool (not closed) on exit — verified empirically that
    `pool.connection()`'s context manager commits on normal exit exactly
    like a bare `psycopg.connect(...)` block does (the property
    app/meter.py::record_usage's `with get_connection() as conn:
    conn.execute(...)` already depends on), so no caller needed to change
    when this became pooled."""
    return _get_pool().connection()


def close_pool() -> None:
    """Shut the pool's background worker threads down cleanly. A pool
    that's never explicitly closed leaves those threads still running at
    process exit — harmless for a short-lived script/CLI invocation, but
    verified empirically to print a "couldn't stop thread... within 5.0
    seconds" warning otherwise. `app/api.py`'s `lifespan` calls this on
    shutdown; a no-op if the pool was never opened (nothing queried
    `query_employees`/wrote to the usage ledger this process)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def query_employees(
    tenant: str,
    department: str | None = None,
    name_contains: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """The one fixed query this module exposes: employees for `tenant`,
    optionally narrowed by an exact `department` match and/or a
    case-insensitive substring match on `name`. Both narrowing params are
    OPTIONAL FILTERS ANDed onto the mandatory tenant scope — neither can
    ever *widen* past it, mirroring app/security.py's "doc_ids narrows,
    never widens" rule for Qdrant's scoped search.

    `limit`, when given, becomes a SQL `LIMIT` — bounding the row count at
    the store, not fetching everything and trimming in Python (a broad
    filter should be paid for once, not in full at the store and then
    discarded — see app/tools.py::_query_employees_impl, which calls this
    with `limit=cap + 1` specifically so it can detect "more rows exist"
    without a second COUNT(*) query).

    Returns a list of {name, department, title, hired_on} dicts — never a
    raw cursor/row-tuple, so a caller can't accidentally depend on column
    order surviving a schema change.
    """
    where = ["tenant = %s"]
    params: list = [tenant]
    if department:
        where.append("department = %s")
        params.append(department)
    if name_contains:
        where.append("name ILIKE %s")
        params.append(f"%{name_contains}%")

    sql = (
        "SELECT name, department, title, hired_on FROM employees "
        f"WHERE {' AND '.join(where)} ORDER BY name"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with get_connection() as conn:
        cur = conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
