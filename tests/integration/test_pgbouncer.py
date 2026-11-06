"""The app's pools against a REAL PgBouncer in transaction mode (specs/022; ENGINEERING_PATTERNS.md 95).

The pooler is the shipped service from `docker-compose.yml` (`tests/containers.py::ensure_pgbouncer`), made stricter than production: it refuses
prepared statements outright and has three server connections for the thirty clients below, so a statement prepared on one server connection
is routinely asked for on another. That is the failure a unit test cannot see and a light manual run does not trigger (a single client keeps
reusing one server connection, so the default `prepare_threshold` of 5 passed a 60-query run and failed ~15% of a heavier one).

Needs Docker (self-skips without it).
"""
import asyncio
import uuid

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from psycopg_pool import AsyncConnectionPool

from app.adapters.outbound.postgres import checkpointing
from app.platform import sql_store
from tests.containers import ensure_pgbouncer

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("pgbouncer")]

CLIENTS = 30
QUERIES = 600


@pytest.fixture(scope="module")
def pooler() -> dict[str, str]:
    return ensure_pgbouncer()


async def _hammer(pool: AsyncConnectionPool) -> list[str]:
    """QUERIES transactions of the SAME statement over the pool; returns the distinct error classes."""
    errors: set[str] = set()

    async def one(i: int) -> None:
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT pg_sleep(0.005), %s::int", [i])
        except psycopg.Error as exc:
            errors.add(type(exc).__name__)

    await asyncio.gather(*(one(i) for i in range(QUERIES)))
    return sorted(errors)


async def _pool(url: str, **kwargs) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=1, max_size=CLIENTS, kwargs=kwargs, open=False)
    await pool.open(wait=True)
    return pool


@pytest.mark.parametrize("kwargs", [{}, {"autocommit": True, "prepare_threshold": 0}], ids=["psycopg-default", "the-old-checkpointer-setting"])
async def test_the_old_settings_fail_here_so_a_pass_below_means_something(pooler, kwargs):
    """The self-check (pattern 88): a test that has never been seen to fail proves nothing. The same workload, with the settings the pools had
    before this change, is refused by the pooler."""
    pool = await _pool(pooler["appdata_database_url"], **kwargs)
    try:
        errors = await _hammer(pool)
    finally:
        await pool.close()

    assert errors, "the workload did not fail with prepared statements on: this test can no longer tell a safe pool from an unsafe one"
    assert set(errors) <= {"DuplicatePreparedStatement", "InvalidSqlStatementName", "ProtocolViolation"}, errors


async def test_the_appdata_pool_the_app_builds_runs_the_same_workload_without_error(pooler, monkeypatch):
    monkeypatch.setattr(sql_store, "APPDATA_DATABASE_URL", pooler["appdata_database_url"])
    monkeypatch.setattr(sql_store, "APPDATA_POOL_MAX_SIZE", CLIENTS)
    monkeypatch.setattr(sql_store, "_pool", None)
    try:
        assert await _hammer(await sql_store._get_pool()) == []
    finally:
        await sql_store.close_pool()


async def test_the_checkpointer_the_app_builds_creates_its_schema_and_saves_and_reads_state_through_the_pooler(pooler, monkeypatch):
    monkeypatch.setattr(checkpointing, "CHECKPOINTER_DATABASE_URL", pooler["checkpointer_database_url"])
    monkeypatch.setattr(checkpointing, "CHECKPOINTER_POOL_MAX_SIZE", CLIENTS)
    adapter = checkpointing.PgCheckpointing()
    saver = await adapter.open()  # AsyncPostgresSaver.setup(): its DDL runs through the pooler too
    errors: set[str] = set()

    async def one(i: int) -> None:
        config = {"configurable": {"thread_id": f"pooler-{uuid.uuid4()}", "checkpoint_ns": ""}}
        try:
            for step in range(4):
                checkpoint = empty_checkpoint()
                config = await saver.aput(config, checkpoint, {"source": "input", "step": step, "writes": {}, "parents": {}}, {})
                assert (await saver.aget_tuple(config)).checkpoint["id"] == checkpoint["id"]
        except psycopg.Error as exc:
            errors.add(type(exc).__name__)

    try:
        await asyncio.gather(*(one(i) for i in range(60)))
    finally:
        await adapter.close()

    assert errors == set()


async def test_thirty_client_connections_are_served_by_three_server_connections(pooler):
    """The point of the pooler: Postgres sees the pooler's pool, not the sum of every process's pools."""
    pool = await _pool(pooler["appdata_database_url"], prepare_threshold=None)
    peak = 0

    async def watch() -> None:
        nonlocal peak
        async with await psycopg.AsyncConnection.connect(pooler["direct_appdata_database_url"], autocommit=True) as direct:
            for _ in range(40):
                row = await (await direct.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = 'appdata' AND usename = 'langfuse' AND pid <> pg_backend_pid()"
                )).fetchone()
                peak = max(peak, row[0])
                await asyncio.sleep(0.02)

    async def work(i: int) -> None:
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_sleep(0.05)")

    try:
        await asyncio.gather(watch(), *(work(i) for i in range(300)))
    finally:
        await pool.close()

    assert 1 <= peak <= 3, f"Postgres saw {peak} connections from {CLIENTS} clients; the pooler is configured for 3"


async def test_a_transaction_local_setting_is_gone_for_the_next_client_on_the_same_server_connection(pooler):
    """Constitution Principle I behind a pooler: the repo's one pattern for state (`set_config(..., true)`, the retention switch) must not
    outlive its transaction, or the next tenant's transaction on that server connection would inherit it."""
    pool = await _pool(pooler["appdata_database_url"], prepare_threshold=None)
    inherited: list = []

    async def one(i: int) -> None:
        async with pool.connection() as conn:
            inherited.append((await (await conn.execute("SELECT current_setting('app.leak', true)")).fetchone())[0])
            await conn.execute("SELECT set_config('app.leak', %s, true)", [f"tenant-{i}"])
            await asyncio.sleep(0.01)
            assert (await (await conn.execute("SELECT current_setting('app.leak')")).fetchone())[0] == f"tenant-{i}"

    try:
        await asyncio.gather(*(one(i) for i in range(200)))
    finally:
        await pool.close()

    assert len(inherited) == 200 and all(value in (None, "") for value in inherited), [v for v in inherited if v][:3]


async def test_get_connection_still_commits_on_exit_rolls_back_on_error_and_enforces_a_unique_key_through_the_pooler(pooler, monkeypatch):
    """Principle IV rests on a row-level unique key inside one transaction (the tool-call guard, the budget holds). A transaction pooler keeps
    that boundary only if one `get_connection()` block is one server transaction, so: a commit on exit, a rollback on an error, a refused
    duplicate. A scratch table with its own name, so nothing here touches a table another test uses."""
    table = f"pooler_probe_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(sql_store, "APPDATA_DATABASE_URL", pooler["appdata_database_url"])
    monkeypatch.setattr(sql_store, "_pool", None)
    try:
        async with sql_store.get_connection() as conn:
            await conn.execute(f"CREATE TABLE {table} (id text PRIMARY KEY)")
        async with sql_store.get_connection() as conn:
            await conn.execute(f"INSERT INTO {table} VALUES ('committed')")
        with pytest.raises(RuntimeError):
            async with sql_store.get_connection() as conn:
                await conn.execute(f"INSERT INTO {table} VALUES ('rolled-back')")
                raise RuntimeError("the block failed after its write")
        with pytest.raises(psycopg.errors.UniqueViolation):
            async with sql_store.get_connection() as conn:
                await conn.execute(f"INSERT INTO {table} VALUES ('committed')")
        async with sql_store.get_connection() as conn:
            rows = await (await conn.execute(f"SELECT id FROM {table}")).fetchall()
        assert [row[0] for row in rows] == ["committed"]
    finally:
        async with sql_store.get_connection() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
        await sql_store.close_pool()


async def test_readiness_goes_through_the_pooler(pooler, monkeypatch):
    from app.adapters.inbound.api import health

    monkeypatch.setattr(health, "CHECKPOINTER_DATABASE_URL", pooler["checkpointer_database_url"])
    monkeypatch.setattr(sql_store, "APPDATA_DATABASE_URL", pooler["appdata_database_url"])
    monkeypatch.setattr(sql_store, "_pool", None)
    try:
        await health._check_checkpointer_postgres()
        await health._check_appdata_postgres()
    finally:
        await sql_store.close_pool()
