"""The `Checkpointing` adapter (`app/ports/checkpointing.py`): LangGraph's `AsyncPostgresSaver` over a connection pool, in a dedicated
checkpointer database.

Postgres, not `build_graph()`'s default `MemorySaver` and not a SQLite file: it is safe under concurrent access from several processes
sharing the same `thread_id`s (the FastAPI service and every `agent_worker.py` replica).

It uses an `AsyncConnectionPool`, not a single `AsyncConnection`, so concurrent turns get independent connections (psycopg serializes all
operations on one connection). The pool is held on the adapter so it is not garbage-collected under the saver (that would close every pooled
connection) and `close` can shut it down on graceful exit.

Workaround: `AsyncPostgresSaver` still wraps every checkpoint read/write in one `asyncio.Lock` per saver regardless of `conn` type,
hard-serializing checkpoint I/O even with a pool (~5x latency growth from N=1 to N=50 concurrent turns; open upstream defect
langchain-ai/langgraph#7259, fix #7269 unmerged). Swapping `saver.lock` for an `asyncio.Semaphore` (same `async with` protocol) is the
community-verified fix, capping concurrency at the pool size instead of 1. Remove once #7269 ships, and re-check before any
langgraph-checkpoint-postgres bump: this pokes a private attribute of a third-party class.

`saver.setup()` is idempotent (creates tables on first run only), so it is safe on every process start.
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import CHECKPOINTER_DATABASE_URL, CHECKPOINTER_POOL_MAX_SIZE


class PgCheckpointing:
    def __init__(self) -> None:
        self._pool: AsyncConnectionPool | None = None

    async def open(self) -> AsyncPostgresSaver:
        pool = AsyncConnectionPool(
            CHECKPOINTER_DATABASE_URL,
            min_size=1,
            max_size=CHECKPOINTER_POOL_MAX_SIZE,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True)
        saver = AsyncPostgresSaver(conn=pool)  # type: ignore[arg-type]  # the stubs cannot see the pool's `row_factory=dict_row` kwarg
        if isinstance(saver.conn, AsyncConnectionPool):
            # Workaround for langchain-ai/langgraph#7259 — see this module's docstring.
            saver.lock = asyncio.Semaphore(CHECKPOINTER_POOL_MAX_SIZE)  # type: ignore[assignment]  # a Semaphore has the same `async with` protocol; it is the upstream fix (#7269)
        await saver.setup()
        self._pool = pool
        return saver

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
