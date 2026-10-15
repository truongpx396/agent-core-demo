# Contract: `credit_billing.migrate` (the Python interface)

For a host or a service shell that applies the schema itself instead of running the command. The command is a thin wrapper over exactly this.

```python
from credit_billing import db, migrate

db.configure(connection=pool.connection)   # the same injection every other module uses; BillingNotConfigured if absent
status = await migrate.status()            # read-only
result = await migrate.migrate(lock_timeout_seconds=10.0)
```

## Functions

| Name | Signature | Contract |
|---|---|---|
| `shipped()` | `() -> list[Script]` | The package's scripts in application order, each `Script(name, checksum)`; checksum is the sha256 hex of the text as shipped. Pure; no database. |
| `statements(name)` | `(str) -> str` | The script's text without its `\connect` line. Raises `ValueError` for any other line beginning with a backslash. |
| `status()` | `async () -> Status` | Reads the record table if it exists; never writes, never creates it. |
| `migrate()` | `async (*, lock_timeout_seconds: float = 10.0) -> Result` | Applies every pending or changed script in order, one transaction each. Raises `MigrationLockTimeout` or `MigrationFailed`; raises `ValueError` if `lock_timeout_seconds <= 0`. |

## Types

```text
Script(name: str, checksum: str)
ScriptStatus(name: str, state: Literal["applied","pending","changed"], checksum: str,
             recorded_checksum: str | None, applied_at: datetime | None)
Status(scripts: list[ScriptStatus], unknown: list[str], record_table_exists: bool)
    .up_to_date -> bool        # every shipped script is "applied"; unknown scripts do not count against it
Result(applied: list[str], skipped: list[str])      # names, in application order

MigrationError(RuntimeError)
  ├─ MigrationLockTimeout(script, seconds)      # str(): says what was waited for and that re-running is safe
  └─ MigrationFailed(script, detail)            # detail is the database's first message line, never a connection string
```

## Behaviour that callers may rely on

- Each script runs in a transaction obtained from the injected `connection` provider, so the provider's rule applies (commit on clean exit, rollback on exception). A caller must inject a provider that opens a fresh transaction per `async with`; passing one shared long-lived transaction defeats the per-script atomicity.
- The order inside one script's transaction is fixed: `SET LOCAL lock_timeout`, the advisory lock, the record table, the in-lock re-check, the script, the record. Tests pin it.
- `migrate()` is safe to call on every service start: on a current database it takes the advisory lock four times for a few milliseconds and changes nothing.
- It never calls `db.configure`, never opens its own pool, and imports nothing from the application.

## Not part of the contract

The record table's name is documented (`billing_schema_migrations`) so an operator can inspect it, but code outside this module must not write to it.
