"""Structured-data access: fixed, parameterized queries against Postgres —
never LLM-generated SQL (GRAPH_PATTERNS.md pattern 21 — the "fixed tools,
never generated queries" note from pattern 15, finally with something to
apply it to).

`query_employees` is the ONLY query this module can run. There is no
`execute(sql: str)` escape hatch, no way to reach this module from
app/agent/tools.py except through that one function's typed, closed parameter
set. That is the entire point: a tool that let the model construct a
WHERE clause (or worse, full SQL text) would be an injection and
exfiltration surface no per-tenant scoping could reliably bound — the
access boundary has to live in code that's reviewable and testable, not
in a string the model assembles. Every query this module issues is
parameterized (`%s` placeholders, psycopg's own escaping) AND always
includes `WHERE tenant = %s` with a value from `SecurityCtx`, never from
caller-supplied text — the same pre-filter discipline
app/retrieval/qdrant_store.py's hybrid_search already applies, just against a
relational store instead of a vector one.
"""
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from app.core.config import APPDATA_DATABASE_URL

# One pool for the process lifetime — every caller (query_employees below,
# app/agent/meter.py's usage_ledger reads/writes) shares it via
# get_connection(), rather than each paying a fresh TCP+auth handshake per
# call the way a bare `psycopg.connect()` per call did before. Opened
# lazily (on first get_connection() call, not at import time) so importing
# this module never implies a network dependency — matches
# app/retrieval/qdrant_store.py's get_client() and
# app/retrieval/semantic_cache.py's _get_client() doing the same for their
# own stores.
#
# `AsyncConnectionPool`, same loop-affinity constraint app/agent/runtime.py's
# `_open_checkpointer` documents for `AsyncPostgresSaver`'s own pool: once
# opened, this pool is bound to whichever event loop was running at that
# first call, and every later `get_connection()` must be awaited from that
# SAME loop. Fine in every real long-lived process this app runs (uvicorn's
# own loop, app/turns/agent_worker.py's main loop, the CLI's one
# `asyncio.run(main())`) — each holds exactly one loop for its whole
# lifetime, and nothing left in this codebase nests a second `asyncio.run()`
# inside an already-running one anymore (see app/agent/subagent_tools.py's
# `run_subagent`, the one caller that used to). Unlike the checkpointer,
# this pool is NOT given its own explicit `init_*_async()` call wired into
# each process's startup — a disclosed, deliberate gap: every real caller
# here already runs deep inside the graph's own already-open loop by the
# time it first queries, so lazy-on-first-use lands on the right loop in
# practice. A test or script that opens its OWN fresh `asyncio.run()` per
# call (this repo's own test convention, see tests/agent/test_graph*.py)
# must not share a real instance of this pool across more than one such
# call — every existing test instead mocks this module's own functions
# rather than opening a real pool, so this has never actually bitten.
_pool: AsyncConnectionPool | None = None


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        pool = AsyncConnectionPool(APPDATA_DATABASE_URL, min_size=1, max_size=10, open=False)
        await pool.open(wait=True)
        _pool = pool
    return _pool


@asynccontextmanager
async def get_connection():
    """A pooled connection, checked out for the caller's `async with` block
    and returned to the pool (not closed) on exit — same `pool.connection()`
    contract as the sync version this replaced (commits on normal exit,
    the property app/agent/meter.py::record_usage's `async with
    get_connection() as conn: await conn.execute(...)` depends on), just
    awaited, and wrapped in its own `@asynccontextmanager` so the pool
    itself is only ever looked up (and lazily opened) from inside a real
    `async with` block, never before one."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        yield conn


async def close_pool() -> None:
    """Shut the pool down cleanly. A pool that's never explicitly closed
    leaves its background worker asyncio tasks still running at process
    exit — harmless for a short-lived script/CLI invocation, but the same
    "leaked pool" concern the sync version's own docstring already
    disclosed. `app/api/main.py`'s `lifespan` calls this on shutdown; a
    no-op if the pool was never opened (nothing queried
    `query_employees`/wrote to the usage ledger this process)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def query_employees(
    tenant: str,
    department: str | None = None,
    name_contains: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """The one fixed query this module exposes: employees for `tenant`,
    optionally narrowed by an exact `department` match and/or a
    case-insensitive substring match on `name`. Both narrowing params are
    OPTIONAL FILTERS ANDed onto the mandatory tenant scope — neither can
    ever *widen* past it, mirroring app/core/security.py's "doc_ids narrows,
    never widens" rule for Qdrant's scoped search.

    `limit`, when given, becomes a SQL `LIMIT` — bounding the row count at
    the store, not fetching everything and trimming in Python (a broad
    filter should be paid for once, not in full at the store and then
    discarded — see app/agent/tools.py::_query_employees_impl, which calls this
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

    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]
