"""Re-applying a `postgres-init/NN-*.sql` migration to the SHARED integration database without causing a deadlock.

Several tests check that a migration "can be applied twice" (an operator re-running it by hand must not fail). Re-running
the DDL is not harmless on a database other xdist workers are writing to: `CREATE INDEX IF NOT EXISTS` takes a SHARE lock on
its table, `CREATE OR REPLACE TRIGGER` a SHARE ROW EXCLUSIVE one, and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` an ACCESS
EXCLUSIVE one even when the column is already there. A concurrent test holding a row lock on one table and waiting for another
forms a cycle with the script, and Postgres kills one side with `DeadlockDetected`. Which side is a matter of timing, so the
failure appeared in an unrelated test (a webhook test failed with `DeadlockDetected` instead of its injected `RuntimeError`).

The fix is to make THIS session the one that always gives up. Postgres runs its deadlock check only after a session has waited
`deadlock_timeout` (1 s by default). A `lock_timeout` shorter than that makes the re-apply abandon a wait before any check runs:
it rolls back, which dissolves the cycle, and the other transaction (waiting for at most `lock_timeout`) simply continues. The
script is idempotent, so trying again is exactly what the test is about. A `DeadlockDetected` is NOT retried: with the timeout in
place it cannot happen, and if it does the assumption is wrong and the test should say so.
"""
import asyncio
import random
from pathlib import Path

import psycopg
from psycopg import errors as pg_errors

INIT_DIR = Path(__file__).resolve().parents[2] / "postgres-init"

# Must stay below Postgres's `deadlock_timeout` (checked by tests/integration/test_schema_reapply_real_postgres.py).
LOCK_TIMEOUT_MS = 300
# 60 tries of up to 300 ms waiting plus a short pause is about 30 s in the worst case: far longer than any other test holds a lock.
ATTEMPTS = 60


def script_sql(name: str) -> str:
    """The migration without its `\\connect` line (psql syntax; the caller is already connected to the right database)."""
    text = (INIT_DIR / name).read_text()
    return "\n".join(line for line in text.splitlines() if not line.startswith("\\connect"))


async def reapply(url: str, name: str, *, attempts: int = ATTEMPTS) -> int:
    """Applies `postgres-init/<name>` to the database at `url` and returns how many tries it took (1 = no contention).

    Each try is a fresh connection and one implicit transaction: a try that times out on a lock is rolled back whole."""
    sql = script_sql(name)
    for attempt in range(1, attempts + 1):
        try:
            async with await psycopg.AsyncConnection.connect(url) as conn:
                await conn.execute(f"SET lock_timeout = {LOCK_TIMEOUT_MS}")
                await conn.execute(sql)
            return attempt
        except pg_errors.LockNotAvailable:
            await asyncio.sleep(0.05 + random.random() * 0.15)  # jitter, so two re-applies do not collide in step
    raise AssertionError(f"{name} could not be re-applied in {attempts} tries: a transaction held its table for far too long")
