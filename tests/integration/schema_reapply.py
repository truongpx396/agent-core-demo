"""Re-applying a `postgres-init/NN-*.sql` migration to the SHARED integration database without a deadlock.

Several tests check a migration "can be applied twice", but re-running DDL isn't harmless while other xdist
workers write: `CREATE INDEX IF NOT EXISTS` takes a SHARE lock, `CREATE OR REPLACE TRIGGER` SHARE ROW
EXCLUSIVE, and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` ACCESS EXCLUSIVE even when the column exists. A test
holding a row lock on one table and waiting for another forms a cycle with the script and Postgres kills one
side with `DeadlockDetected`, by timing, so it surfaced in an unrelated test (a webhook test failed with
`DeadlockDetected` instead of its injected `RuntimeError`).

The fix makes THIS session always give up: Postgres runs its deadlock check only after a wait of
`deadlock_timeout` (1 s by default), so a shorter `lock_timeout` abandons the wait first, rolls back
(dissolving the cycle) and lets the other transaction continue. The script is idempotent, so trying again is
the point. A `DeadlockDetected` is NOT retried: with the timeout it can't happen, and if it does the
assumption is wrong and the test should say so.
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
    """Applies `postgres-init/<name>` to the database at `url` and returns how many tries it took (1 = no
    contention).
    """
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
