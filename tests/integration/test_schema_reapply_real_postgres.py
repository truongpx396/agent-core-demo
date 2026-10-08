"""`tests/integration/schema_reapply.py` against a REAL Postgres: re-applying a migration never makes anyone else the victim.

The flake this prevents (found 2026-10-08, one failure in about seven parallel runs of the webhook tests) was a lock cycle between a
test re-applying migration 22 and a test inserting into the same tables. The cycle is built here on purpose, without any timing
luck: a second connection takes a lock the script needs, waits until the script is actually blocked on it (read from `pg_locks`),
and then asks for a lock the script is already holding. Without the helper's `lock_timeout` that is a deadlock, and Postgres kills
one of the two within a second.
"""
import asyncio
from contextlib import asynccontextmanager

import psycopg
import pytest

from tests.containers import ensure_postgres
from tests.integration import schema_reapply

pytestmark = pytest.mark.integration

# Every re-apply here is bounded. Without the helper's `lock_timeout` a re-apply waits on a held lock for as long as the holder
# keeps it, which is a test that hangs a CI job instead of one that fails; a bound turns that into a failure with a name.
BOUND_SECONDS = 20


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@asynccontextmanager
async def _transaction(url):
    async with await psycopg.AsyncConnection.connect(url) as conn:
        yield conn


async def _a_session_is_waiting_for(url: str, table: str, *, seconds: float = 5.0) -> None:
    """Returns once some session is blocked on a lock of `table` (so the cycle below is built in the order we mean)."""
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as control:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            cur = await control.execute(
                "SELECT count(*) FROM pg_locks WHERE NOT granted AND relation = %s::regclass", (table,)
            )
            if (await cur.fetchone())[0]:
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"nothing ever waited for {table}: the test did not build the lock order it means to build")


async def test_the_timeout_is_shorter_than_postgres_deadlock_check(appdata_url):
    """The whole argument rests on this: the re-apply must give up BEFORE the deadlock detector would run."""
    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        cur = await conn.execute("SELECT setting::int FROM pg_settings WHERE name = 'deadlock_timeout'")  # reported in ms
        (deadlock_timeout_ms,) = await cur.fetchone()

    assert schema_reapply.LOCK_TIMEOUT_MS < deadlock_timeout_ms


@pytest.mark.parametrize("script", ["21-usage-event-credits.sql", "22-billing.sql", "23-usage-export-outbox.sql"])
async def test_each_migration_can_be_reapplied(appdata_url, script):
    assert await asyncio.wait_for(schema_reapply.reapply(appdata_url, script), BOUND_SECONDS) >= 1


async def test_a_reapply_waits_out_an_open_transaction_instead_of_failing(appdata_url):
    async with _transaction(appdata_url) as holder:
        await holder.execute("LOCK TABLE billing_webhook_events IN ROW EXCLUSIVE MODE")
        task = asyncio.create_task(schema_reapply.reapply(appdata_url, "22-billing.sql"))
        await _a_session_is_waiting_for(appdata_url, "billing_webhook_events")
        await asyncio.sleep(schema_reapply.LOCK_TIMEOUT_MS / 1000 * 2)  # long enough to have timed out at least once
        assert not task.done(), "it must keep trying, not give up while the table is still held"
    # the holder committed on leaving the block

    assert await asyncio.wait_for(task, BOUND_SECONDS) > 1, "it needed more than one try, which is the retry being used"


async def test_a_lock_cycle_with_another_transaction_never_makes_that_transaction_the_victim(appdata_url):
    """The script takes a lock on `credit_lots` and then wants `billing_webhook_events` (migration 22, in that order). The other
    transaction holds the second and then asks for the first: a cycle. Without the helper's `lock_timeout`, Postgres raises
    `DeadlockDetected` in one of them. With it, the script times out first and the other transaction just continues."""
    async with _transaction(appdata_url) as other:
        await other.execute("LOCK TABLE billing_webhook_events IN ROW EXCLUSIVE MODE")
        task = asyncio.create_task(schema_reapply.reapply(appdata_url, "22-billing.sql"))
        await _a_session_is_waiting_for(appdata_url, "billing_webhook_events")  # the script now holds credit_lots and waits here

        await other.execute("LOCK TABLE credit_lots IN ROW EXCLUSIVE MODE")  # completes the cycle; must NOT raise DeadlockDetected

    assert await asyncio.wait_for(task, BOUND_SECONDS) >= 1


async def test_two_reapplies_at_once_both_finish(appdata_url):
    first, second = await asyncio.wait_for(
        asyncio.gather(
            schema_reapply.reapply(appdata_url, "22-billing.sql"), schema_reapply.reapply(appdata_url, "23-usage-export-outbox.sql")
        ),
        BOUND_SECONDS,
    )

    assert first >= 1 and second >= 1


async def test_a_table_that_is_never_released_is_reported_not_waited_on_forever(appdata_url):
    async with _transaction(appdata_url) as holder:
        await holder.execute("LOCK TABLE billing_webhook_events IN ROW EXCLUSIVE MODE")

        with pytest.raises(AssertionError, match="could not be re-applied"):
            await asyncio.wait_for(schema_reapply.reapply(appdata_url, "22-billing.sql", attempts=2), BOUND_SECONDS)
