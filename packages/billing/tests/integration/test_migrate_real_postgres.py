"""The migration tool against a REAL Postgres (credit_billing/migrate.py, spec 012-billing-migrate-idempotency).

`tests/test_migrate.py` pins the statements the tool sends; what the DATABASE does with them is the point of a migration tool and is proven here:
the advisory lock that serialises runners (eight concurrent `CREATE TABLE IF NOT EXISTS` failed 216 times in 40 rounds, research E4), the
`lock_timeout` that turns a busy table into an error instead of a hang (E1), transactional DDL that leaves nothing half-applied, and the
adoption of a database the host's init scripts created.

Each test gets its OWN database inside the shared container (`CREATE DATABASE migtest_...`, dropped afterwards), so nothing here touches `appdata`
and re-applying DDL cannot deadlock with another test's writes (the repo's pattern 60). Every wait that could hang is bounded with
`asyncio.wait_for`, so a regression fails the test instead of the run.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

import credit_billing
from credit_billing import credits, migrate, schema
from tests.containers import ensure_postgres
from tests.integration.databases import (
    NAMES,
    WALLET_TABLES,
    Throwaway,
    create_database,
    init_like_the_host,
    open_connection,
)

pytestmark = pytest.mark.integration

@pytest.fixture(scope="module")
def container_url() -> str:
    return ensure_postgres()["appdata_database_url"]


async def make_database(container_url: str, monkeypatch) -> Throwaway:
    """A throwaway database that the migration module is pointed at."""
    database = await create_database(container_url)
    monkeypatch.setattr(migrate, "get_connection", database.connection)
    return database


@pytest.fixture
async def database(container_url, monkeypatch):
    created = await make_database(container_url, monkeypatch)
    yield created
    await created.drop()


def run_cli(database: Throwaway, tmp_path: Path, *args: str, url: str | None = None):
    """The installed command as a real process, the way an operator or a pipeline runs it (not the in-process `main`): its own interpreter,
    its own environment, no `.env` in the working directory."""
    env = {
        **os.environ,
        "APPDATA_DATABASE_URL": url or database.url,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "PYTHONPATH": str(Path(credit_billing.__file__).resolve().parents[1]),
    }
    done = subprocess.run(
        [sys.executable, "-m", "credit_billing.cli.migrate", *args], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=90, check=False
    )
    return done.returncode, done.stdout, done.stderr


# --- a blank database --------------------------------------------------------------------------------------------------

async def test_a_blank_database_gets_both_scripts_applied_and_recorded_and_a_second_run_changes_nothing(database):
    first = await migrate.migrate()

    assert first.applied == NAMES and first.skipped == []
    for table in (*WALLET_TABLES, "billing_customers", "billing_schema_migrations"):
        assert await database.exists(table), table
    recorded = await database.rows("SELECT script, checksum, applied_at FROM billing_schema_migrations ORDER BY script")
    assert [(name, checksum) for name, checksum, _ in recorded] == [(s.name, s.checksum) for s in migrate.shipped()]

    second = await migrate.migrate()

    assert second.applied == [] and second.skipped == NAMES
    assert await database.rows("SELECT script, checksum, applied_at FROM billing_schema_migrations ORDER BY script") == recorded, (
        "a run that has nothing to do rewrites nothing, applied_at included"
    )
    status = await migrate.status()
    assert status.up_to_date and status.record_table_exists and status.unknown == []


async def test_the_record_table_is_what_the_data_model_says(database):
    await migrate.migrate()

    columns = await database.rows(
        "SELECT column_name, data_type, is_nullable, column_default IS NOT NULL FROM information_schema.columns "
        "WHERE table_name = 'billing_schema_migrations' ORDER BY ordinal_position"
    )
    assert columns == [("script", "text", "NO", False), ("checksum", "text", "NO", False), ("applied_at", "timestamp with time zone", "NO", True)], (
        "exactly these three columns: no tenant, because it describes the schema and not any tenant's data"
    )
    assert await database.scalar(
        "SELECT count(*) FROM information_schema.table_constraints WHERE table_name = 'billing_schema_migrations' AND constraint_type = 'PRIMARY KEY'"
    ) == 1
    with pytest.raises(psycopg.errors.CheckViolation):
        async with database.connection() as conn:
            await conn.execute("INSERT INTO billing_schema_migrations (script, checksum) VALUES ('', 'x')")
    with pytest.raises(psycopg.errors.CheckViolation):
        async with database.connection() as conn:
            await conn.execute("INSERT INTO billing_schema_migrations (script, checksum) VALUES ('x', '')")


async def test_status_on_a_database_that_was_never_migrated_creates_nothing(database):
    status = await migrate.status()

    assert [s.state for s in status.scripts] == ["pending", "pending"] and status.record_table_exists is False
    assert not await database.exists("billing_schema_migrations")


# --- a database the host initialised (adoption) ---------------------------------------------------------------------------

async def test_a_database_the_host_initialised_is_adopted_without_touching_a_single_row(database):
    await init_like_the_host(database)
    async with database.connection() as conn:
        await credits.grant_in(conn, "acme", 500, source="purchase", idempotency_key="k1", actor="test", provider="stripe", external_ref="pi_1")
        await credits.debit_in(conn, "acme", 120, idempotency_key="u1", usage_event_id="u1", reason="model call")
    before = await database.snapshot()
    assert all(before[table] for table in WALLET_TABLES), "the snapshot has real rows to compare"
    assert [s.state for s in (await migrate.status()).scripts] == ["pending", "pending"], "recorded nothing, so pending even though the schema exists"

    result = await migrate.migrate()

    assert result.applied == NAMES, "re-applying an idempotent script IS the adoption"
    assert await database.snapshot() == before
    assert (await migrate.status()).up_to_date
    async with database.connection() as conn:
        assert await credits.verify_in(conn, "acme") == [], "the ledger still agrees with its lots"


@pytest.mark.parametrize("name", NAMES)
async def test_each_shipped_script_can_be_applied_twice_in_a_row_on_a_database_that_has_both(database, name):
    """The premise of the whole model (repeatable scripts): a script that cannot be applied twice must not be edited in place. Both scripts
    are applied first, in order, because `22-billing.sql` references `credit_lots` from `20-credit-wallet.sql` and cannot run alone: the
    order the tool applies them in is a dependency, not a convention."""
    await init_like_the_host(database)
    for _ in range(2):
        async with database.connection() as conn:
            await conn.execute(schema.executable(name))


async def test_the_billing_script_cannot_be_applied_before_the_wallet_script_so_the_order_is_a_dependency(database):
    async with database.connection() as conn:
        with pytest.raises(psycopg.errors.UndefinedTable, match="credit_lots"):
            await conn.execute(schema.executable("22-billing.sql"))


# --- drift --------------------------------------------------------------------------------------------------------------------

async def test_a_changed_checksum_is_reported_applied_again_and_restored_and_check_follows(database, tmp_path):
    await migrate.migrate()
    async with database.connection() as conn:
        await conn.execute("UPDATE billing_schema_migrations SET checksum = 'tampered' WHERE script = '22-billing.sql'")
    wallet_applied_at = await database.scalar("SELECT applied_at FROM billing_schema_migrations WHERE script = '20-credit-wallet.sql'")

    states = {s.name: s.state for s in (await migrate.status()).scripts}
    code_before, out_before, _ = run_cli(database, tmp_path, "--check")
    result = await migrate.migrate()
    code_after, _, _ = run_cli(database, tmp_path, "--check")

    assert states == {"20-credit-wallet.sql": "applied", "22-billing.sql": "changed"}
    assert code_before == 1 and "changed  22-billing.sql" in out_before
    assert result.applied == ["22-billing.sql"] and result.skipped == ["20-credit-wallet.sql"]
    assert code_after == 0
    assert await database.scalar("SELECT checksum FROM billing_schema_migrations WHERE script = '22-billing.sql'") == migrate.shipped()[1].checksum
    assert await database.scalar("SELECT applied_at FROM billing_schema_migrations WHERE script = '20-credit-wallet.sql'") == wallet_applied_at, (
        "the script that did not change was not touched"
    )


async def test_a_script_recorded_by_a_newer_version_is_reported_unknown_left_alone_and_does_not_fail_the_check(database, tmp_path):
    await migrate.migrate()
    async with database.connection() as conn:
        await conn.execute("INSERT INTO billing_schema_migrations (script, checksum) VALUES ('99-from-a-newer-version.sql', 'abc')")

    status = await migrate.status()
    code, out, _ = run_cli(database, tmp_path, "--check")
    await migrate.migrate()

    assert status.unknown == ["99-from-a-newer-version.sql"] and status.up_to_date
    assert code == 0 and "unknown  99-from-a-newer-version.sql" in out
    assert await database.scalar("SELECT count(*) FROM billing_schema_migrations WHERE script = '99-from-a-newer-version.sql'") == 1


# --- concurrency ----------------------------------------------------------------------------------------------------------------

async def test_runners_started_together_apply_each_script_exactly_once(container_url, monkeypatch):
    """Five rounds of four concurrent runners on a blank database. Without the advisory lock taken first this fails with DuplicateTable,
    DuplicateObject or UniqueViolation (research E4); with it, the runners that waited find the work done and skip it."""
    for _round in range(5):
        database = await make_database(container_url, monkeypatch)
        try:
            results = await asyncio.wait_for(asyncio.gather(*[migrate.migrate() for _ in range(4)]), timeout=120)
            assert sum(len(r.applied) for r in results) == 2, f"each of the two scripts was applied once in total, not {[r.applied for r in results]}"
            assert sum(len(r.skipped) for r in results) == 6
            assert await database.scalar("SELECT count(*) FROM billing_schema_migrations") == 2
            assert (await migrate.status()).up_to_date
        finally:
            await database.drop()


async def test_the_migration_lock_and_a_tenant_lock_are_different_key_spaces(database):
    """The wallet locks `pg_advisory_xact_lock(hashtextextended(tenant, 0))` (one bigint). The tool uses the two-integer form, which the
    documentation says is a separate key space, so no tenant name can ever block a migration or be blocked by one."""
    first, second = migrate.LOCK_KEYS
    as_one_bigint = (first << 32) | second
    holder = await open_connection(database)
    prober = await open_connection(database)
    try:
        await holder.execute("SELECT pg_advisory_xact_lock(%s)", (as_one_bigint,))
        cur = await prober.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (first, second))
        assert (await cur.fetchone())[0] is True, "the migration lock is not blocked by a bigint lock with the same numeric value"
        await prober.rollback()
        await holder.rollback()

        await holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (first, second))
        cur = await prober.execute("SELECT pg_try_advisory_xact_lock(%s)", (as_one_bigint,))
        assert (await cur.fetchone())[0] is True, "and a bigint lock is not blocked by the migration lock"
        cur = await prober.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", ("acme",))
        assert (await cur.fetchone())[0] is True, "nor is an ordinary tenant lock"
        await prober.rollback()
    finally:
        await holder.close()
        await prober.close()


# --- a busy table, and a failing script -------------------------------------------------------------------------------------------

async def test_a_table_held_by_another_session_stops_the_run_within_the_wait_and_records_nothing(database):
    await migrate.migrate()
    async with database.connection() as conn:
        await conn.execute("UPDATE billing_schema_migrations SET checksum = 'stale' WHERE script = '20-credit-wallet.sql'")
    holder = await open_connection(database)
    try:
        await holder.execute("LOCK TABLE credit_transactions IN ACCESS EXCLUSIVE MODE")  # held until rollback: the script's trigger DDL needs this table

        started = time.monotonic()
        with pytest.raises(migrate.MigrationLockTimeout) as raised:
            await asyncio.wait_for(migrate.migrate(lock_timeout_seconds=1), timeout=30)
        elapsed = time.monotonic() - started

        assert raised.value.script == "20-credit-wallet.sql" and "running again is safe" in str(raised.value)
        assert elapsed < 6, f"gave up after about the configured second (took {elapsed:.1f}s), not at the deadlock detector's pace"
        assert await database.scalar("SELECT checksum FROM billing_schema_migrations WHERE script = '20-credit-wallet.sql'") == "stale", (
            "the script was rolled back whole: its record is exactly as it was"
        )
    finally:
        await holder.rollback()
        await holder.close()

    result = await asyncio.wait_for(migrate.migrate(lock_timeout_seconds=5), timeout=30)

    assert result.applied == ["20-credit-wallet.sql"], "after the lock is released the same run succeeds"
    assert (await migrate.status()).up_to_date


async def test_a_runner_waiting_for_another_gives_up_at_its_own_timeout_instead_of_waiting_for_ever(database):
    """The advisory lock wait is bounded too (research E1), not only the table locks."""
    first, second = migrate.LOCK_KEYS
    holder = await open_connection(database)
    try:
        await holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (first, second))

        started = time.monotonic()
        with pytest.raises(migrate.MigrationLockTimeout):
            await asyncio.wait_for(migrate.migrate(lock_timeout_seconds=1), timeout=30)

        assert time.monotonic() - started < 6
        assert not await database.exists("billing_schema_migrations"), "it never got far enough to create the record table"
    finally:
        await holder.rollback()
        await holder.close()


async def test_a_script_that_fails_midway_is_rolled_back_whole_and_the_earlier_script_stays_recorded(database, monkeypatch):
    real = migrate.shipped()[0]
    broken = migrate.Script("30-broken.sql", "b" * 64)
    monkeypatch.setattr(migrate, "shipped", lambda: [real, broken])
    original = migrate.statements
    monkeypatch.setattr(
        migrate, "statements", lambda name: "CREATE TABLE migtest_half_done (x int); SELECT 1/0;" if name == broken.name else original(name)
    )

    with pytest.raises(migrate.MigrationFailed) as raised:
        await migrate.migrate()

    assert raised.value.script == "30-broken.sql" and raised.value.detail == "division by zero"
    assert not await database.exists("migtest_half_done"), "the CREATE TABLE before the failing statement was rolled back with it"
    assert await database.rows("SELECT script FROM billing_schema_migrations ORDER BY script") == [("20-credit-wallet.sql",)]


# --- the installed command, as a real process ---------------------------------------------------------------------------------------

async def test_the_command_as_a_process_reports_without_writing_then_applies_then_is_current(database, tmp_path):
    code, out, err = run_cli(database, tmp_path, "--status")
    assert code == 0 and len([line for line in out.splitlines() if line.startswith("pending ")]) == 2 and "No migration record" in out, (out, err)
    assert not await database.exists("billing_schema_migrations"), "--status wrote nothing, not even the record table"

    code, out, err = run_cli(database, tmp_path, "--check")
    assert code == 1, (out, err)

    code, out, err = run_cli(database, tmp_path)
    assert code == 0 and "2 applied, 0 already current." in out, (out, err)
    assert await database.exists("billing_schema_migrations") and await database.exists("credit_transactions")

    code, out, _ = run_cli(database, tmp_path)
    assert code == 0 and "0 applied, 2 already current." in out
    code, _, _ = run_cli(database, tmp_path, "--check")
    assert code == 0


async def test_the_command_as_a_process_exits_3_for_an_unreachable_database_without_the_password(database, tmp_path):
    started = time.monotonic()
    code, out, err = run_cli(database, tmp_path, url="postgresql://billing_user:s3cret-pw@127.0.0.1:1/appdata")

    assert code == 3 and out == "" and err.count("error:") == 1 and "Traceback" not in err
    assert "s3cret-pw" not in err and "billing_user" not in err
    assert time.monotonic() - started < 40


async def test_the_command_as_a_process_exits_4_when_a_table_is_busy(database, tmp_path):
    await migrate.migrate()
    async with database.connection() as conn:
        await conn.execute("UPDATE billing_schema_migrations SET checksum = 'stale' WHERE script = '20-credit-wallet.sql'")
    holder = await open_connection(database)
    try:
        await holder.execute("LOCK TABLE credit_transactions IN ACCESS EXCLUSIVE MODE")

        code, out, err = await asyncio.to_thread(run_cli, database, tmp_path, "--lock-timeout-seconds", "1")

        assert code == 4 and out == "" and err.startswith("error: 20-credit-wallet.sql did not complete: could not get a lock within 1s"), err
        assert "Traceback" not in err
    finally:
        await holder.rollback()
        await holder.close()
