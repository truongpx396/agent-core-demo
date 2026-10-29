"""Applies the package's shipped schema scripts to an existing database and records that it did
(specs/012-billing-migrate-idempotency).

The schema could only be applied by `psql`, once (a fresh volume's init, or an operator by hand): nothing
recorded what ran, nothing could answer "is this database current?", and two deploys starting together could
collide. A service that owns its schema and runs against a database holding money needs all three.

## The model: repeatable scripts

The shipped scripts are idempotent (`CREATE ... IF NOT EXISTS`, `CREATE OR REPLACE`) and edited in place as
the schema evolves: Flyway's *repeatable migration*, applied when never applied and again whenever the
checksum changes. A script is `pending` (never recorded), `applied` (recorded with this checksum) or `changed`
(recorded with another). A recorded script this package doesn't ship is `unknown`: a newer version ran here,
so it is reported, left alone and never fails a check (a rollback to an older package isn't blocked).
Immutable numbered migrations are the next step for a change that can't be idempotent or needs a backfill; not
built, and the record table doesn't preclude them.

Adopting a database the host initialised needs no special case: the init scripts recorded nothing, so every
script is `pending`, applying one to a database that has it changes no row, and then it is recorded.

## The order inside one script's transaction (each step answers a failure)

1. `SET LOCAL lock_timeout` bounds every wait below, the advisory lock included. A timeout is a clean
   rollback; the deadlock detector would pick a victim among unrelated sessions (the repo's pattern 60).
2. `pg_advisory_xact_lock(k1, k2)` BEFORE anything else: `CREATE TABLE IF NOT EXISTS` is not a concurrency
   guard (eight connections creating one table failed 216 times in 40 rounds). The two-integer form is a
   separate key space from the wallet's single-bigint per-tenant lock, so no tenant name can collide, and
   being transaction-level a killed process can't leave it held.
3. The record table, `CREATE TABLE IF NOT EXISTS`, inside the lock.
4. The check, inside the lock: a runner that waited finds the script already recorded with this checksum and
   skips it.
5. The script, as one multi-statement string (no parameters, so the simple-query protocol).
6. The record, `INSERT ... ON CONFLICT (script) DO UPDATE`.

One transaction per script (PostgreSQL has transactional DDL) means a script and its record commit together or
not at all and earlier scripts stay recorded. The transaction is the connection provider's
(`credit_billing.db`: one `async with` is one transaction), so every error is raised from INSIDE the block.

Not done: downgrades, a dry run beyond `status()`, detecting a database altered by hand (a checksum covers the
script, not the live schema). It never runs by itself, never opens a pool and never holds a connection string.

Imports nothing from `app` (enforced by tests/test_boundary.py and the host's
tests/billing/test_billing_core_boundary.py).
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg import errors as pg_errors

from credit_billing import schema
from credit_billing.db import get_connection

logger = logging.getLogger(__name__)

LEDGER_TABLE = "billing_schema_migrations"
# The two-integer advisory form: a key space of its own, documented as not overlapping the single-bigint form the wallet takes per tenant.
# The first number spells "cbm1".
LOCK_KEYS = (0x63626D31, 1)
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0

_CREATE_LEDGER = (
    "CREATE TABLE IF NOT EXISTS billing_schema_migrations ("
    "script TEXT PRIMARY KEY CHECK (script <> ''), "
    "checksum TEXT NOT NULL CHECK (checksum <> ''), "
    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)

State = Literal["applied", "pending", "changed"]


@dataclass(frozen=True)
class Script:
    """A shipped script: its file name and the sha256 (hex) of its bytes as shipped, `\\connect` line included."""

    name: str
    checksum: str


@dataclass(frozen=True)
class ScriptStatus:
    name: str
    state: State
    checksum: str  # what is shipped
    recorded_checksum: str | None  # what the database says it applied, if anything
    applied_at: datetime | None


@dataclass(frozen=True)
class Status:
    scripts: list[ScriptStatus]
    unknown: list[str]  # recorded, but not shipped by this version
    record_table_exists: bool

    @property
    def up_to_date(self) -> bool:
        """Every shipped script is applied. An unknown recorded script does not count against it (a newer version ran here)."""
        return all(script.state == "applied" for script in self.scripts)


@dataclass(frozen=True)
class Result:
    applied: list[str]  # applied (or re-applied) by this call
    skipped: list[str]  # already recorded with the shipped checksum


class MigrationError(RuntimeError):
    """A migration did not complete. Nothing from the script it names was applied or recorded."""


class MigrationLockTimeout(MigrationError):
    def __init__(self, script: str, seconds: float) -> None:
        self.script, self.seconds = script, seconds
        super().__init__(
            f"{script} did not complete: could not get a lock within {seconds:g}s (another transaction holds a table this script needs, "
            "or another migration is running). Nothing from this script was applied; running again is safe."
        )


class MigrationFailed(MigrationError):
    def __init__(self, script: str, detail: str) -> None:
        self.script, self.detail = script, detail
        super().__init__(f"{script} did not complete: {detail}. Nothing from this script was applied.")


def shipped() -> list[Script]:
    """The shipped scripts in the order to apply them. The checksum is over the bytes as shipped, so it equals `sha256sum` of the file and of
    the host's byte-equal copy."""
    return [Script(name, hashlib.sha256(schema.read_bytes(name)).hexdigest()) for name in schema.scripts()]


def statements(name: str) -> str:
    """The script as plain SQL (its psql `\\connect` line removed). See `schema.executable` for the rule."""
    return schema.executable(name)


async def status() -> Status:
    """What is applied, pending, changed and unknown. Reads only: it never creates the record table, so a database that has never been
    migrated reports every script as pending (which includes one a host's init scripts created; applying is safe there)."""
    async with get_connection() as conn:
        cur = await conn.execute("SELECT to_regclass(%s::text) IS NOT NULL", (LEDGER_TABLE,))
        row = await cur.fetchone()
        exists = bool(row and row[0])
        recorded: dict[str, tuple[str, datetime]] = {}
        if exists:
            cur = await conn.execute("SELECT script, checksum, applied_at FROM billing_schema_migrations")
            recorded = {name: (checksum, applied_at) for name, checksum, applied_at in await cur.fetchall()}
    scripts: list[ScriptStatus] = []
    for script in shipped():
        if script.name not in recorded:
            scripts.append(ScriptStatus(script.name, "pending", script.checksum, None, None))
            continue
        checksum, applied_at = recorded[script.name]
        scripts.append(ScriptStatus(script.name, "applied" if checksum == script.checksum else "changed", script.checksum, checksum, applied_at))
    known = {script.name for script in scripts}
    return Status(scripts, sorted(name for name in recorded if name not in known), exists)


def _detail(exc: psycopg.Error) -> str:
    """The first line of the database's own message. A connection-level failure gets a fixed sentence instead: its message can name the user."""
    if isinstance(exc, psycopg.OperationalError):
        return "the database connection failed"
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


async def _apply(script: Script, milliseconds: int, seconds: float) -> bool:
    """Applies one script in its own transaction. True if it ran, False if it was already recorded with this checksum."""
    try:
        sql = statements(script.name)
    except ValueError as exc:  # a meta-command this tool cannot apply: caught before a connection is checked out
        raise MigrationFailed(script.name, str(exc)) from exc
    async with get_connection() as conn:
        try:
            await conn.execute(f"SET LOCAL lock_timeout = '{milliseconds}ms'")
            await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", LOCK_KEYS)
            await conn.execute(_CREATE_LEDGER)
            cur = await conn.execute("SELECT checksum FROM billing_schema_migrations WHERE script = %s", (script.name,))
            row = await cur.fetchone()
            if row is not None and row[0] == script.checksum:
                logger.info("billing_migrate_skipped", extra={"script": script.name, "checksum": script.checksum})
                return False
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO billing_schema_migrations (script, checksum) VALUES (%s, %s) "
                "ON CONFLICT (script) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = now()",
                (script.name, script.checksum),
            )
        except pg_errors.LockNotAvailable as exc:
            raise MigrationLockTimeout(script.name, seconds) from exc
        except psycopg.Error as exc:
            raise MigrationFailed(script.name, _detail(exc)) from exc
    logger.info("billing_migrate_applied", extra={"script": script.name, "checksum": script.checksum})
    return True


async def migrate(*, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> Result:
    """Applies every pending or changed shipped script, in order, one transaction each. Safe to run any number of times and from any number
    of processes at once. Raises MigrationLockTimeout or MigrationFailed; the scripts before the one that failed stay applied."""
    if not lock_timeout_seconds > 0:
        raise ValueError(f"lock_timeout_seconds must be above zero, got {lock_timeout_seconds}")
    milliseconds = max(1, round(lock_timeout_seconds * 1000))
    applied: list[str] = []
    skipped: list[str] = []
    for script in shipped():
        (applied if await _apply(script, milliseconds, lock_timeout_seconds) else skipped).append(script.name)
    return Result(applied, skipped)
