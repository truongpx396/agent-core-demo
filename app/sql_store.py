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
import psycopg

from app.config import APPDATA_DATABASE_URL


def get_connection() -> psycopg.Connection:
    """A fresh connection per call — this app's traffic is low enough
    (a local demo, not a production connection-pool workload) that a
    pool would be premature; `psycopg.connect` is cheap enough here that
    adding one would be optimizing a cost that doesn't exist yet."""
    return psycopg.connect(APPDATA_DATABASE_URL)


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
