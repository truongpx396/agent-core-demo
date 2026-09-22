"""Structured-data access: fixed, parameterized queries against Postgres —
never LLM-generated SQL (pattern 21).

`query_employees` is the ONLY query this module can run — no
`execute(sql: str)` escape hatch. A tool that let the model construct a
WHERE clause (or full SQL text) would be an injection/exfiltration surface
no per-tenant scoping could bound, so the access boundary lives in
reviewable code, not a string the model assembles. Every query is
parameterized (`%s`, psycopg's escaping) and always includes `WHERE
tenant = %s` from `SecurityCtx`, never caller-supplied text — same
pre-filter discipline as `qdrant_store.py::hybrid_search`.
"""
import asyncio
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from app.core.config import APPDATA_DATABASE_URL

# One pool for the process lifetime, shared by every caller via
# get_connection() rather than a fresh TCP+auth handshake per call. Opened
# lazily on first get_connection() call, not at import time, so importing
# this module never implies a network dependency (matches
# qdrant_store.py/semantic_cache.py's own lazy clients).
#
# `AsyncConnectionPool`, same loop-affinity constraint as
# runtime.py::_open_checkpointer's pool: bound to whichever loop was
# running at first open, and every later get_connection() must be awaited
# from that SAME loop. Fine for every real long-lived process here (each
# holds one loop for its lifetime). No explicit init_*_async() call wired
# into startup — a deliberate gap, since every real caller already runs
# inside the graph's open loop by first query. Tests mock this module's
# functions rather than sharing a real pool across separate asyncio.run()
# calls.
_pool: AsyncConnectionPool | None = None
# Guards _pool's construction, not its use: `await pool.open(wait=True)`
# below means the `if _pool is None` check and the `_pool = pool` write
# are NOT atomic w.r.t. the event loop — without this lock, several
# concurrent first-callers (exactly what agent_worker.py's own
# AGENT_WORKER_MAX_CONCURRENCY produces right after a fresh process starts
# serving its first batch of turns, before anything has opened this pool
# yet) could each pass the None check, each construct and open their OWN
# AsyncConnectionPool, and each overwrite `_pool` — every loser's pool is
# then unreachable (close_pool() only ever sees whichever one `_pool`
# currently points to) and leaks up to `max_size` live Postgres
# connections for the rest of the process's life. The lock makes a losing
# caller AWAIT the winner instead of repeating its work.
_pool_lock = asyncio.Lock()


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:  # re-check: another caller may have finished while this one waited for the lock
            pool = AsyncConnectionPool(APPDATA_DATABASE_URL, min_size=1, max_size=10, open=False)
            await pool.open(wait=True)
            _pool = pool
    return _pool


@asynccontextmanager
async def get_connection():
    """A pooled connection, checked out and returned (not closed) on
    exit — commits on normal exit, which `usage_ledger.py::record_usage`
    depends on. Wrapped in its own `@asynccontextmanager` so the pool is
    only looked up (and lazily opened) from inside a real `async with`
    block."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        yield conn


async def close_pool() -> None:
    """Shut the pool down cleanly — an unclosed pool leaves background
    asyncio tasks running at process exit. `app/api/main.py`'s `lifespan`
    calls this on shutdown; a no-op if the pool was never opened."""
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
    optionally narrowed by exact `department` and/or substring `name`
    match. Both are optional filters ANDed onto the mandatory tenant
    scope — neither can widen past it (mirrors `security.py`'s "doc_ids
    narrows, never widens" rule).

    `limit`, when given, becomes a SQL `LIMIT` at the store rather than
    trimming in Python — `tools.py::_query_employees_impl` calls this with
    `limit=cap + 1` to detect "more rows exist" without a second query.

    Returns {name, department, title, hired_on} dicts, never a raw
    cursor/tuple.
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
