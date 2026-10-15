# Contract: `credit-billing-migrate`

The operator command that applies the package's shipped schema scripts to an existing database. Console script of the `credit-billing` distribution; equivalent to `python -m credit_billing.cli.migrate`; in the monorepo, `make billing-migrate` and `make billing-migrate-status`.

## Synopsis

```text
credit-billing-migrate [--status | --check] [--lock-timeout-seconds N]
```

| Argument | Meaning |
|---|---|
| *(none)* | Apply every shipped script that is pending or changed, in order, then print what was done. |
| `--status` | Print the state of every shipped script and every unknown recorded script. Writes nothing (does not even create the record table). Exit 0. |
| `--check` | Same output as `--status`; exit 1 if any shipped script is pending or changed, else 0. Writes nothing. A deploy gate. |
| `--lock-timeout-seconds N` | How long any single lock wait may last before the run gives up (default 10; must be above 0). |

`--status` and `--check` are mutually exclusive. The connection comes from the environment exactly as in the other operator commands: `APPDATA_DATABASE_URL` (default `postgresql://langfuse:langfuse@localhost:5432/appdata`), read from the environment and from a `.env` in the working directory. `OTEL_EXPORTER_OTLP_ENDPOINT` behaves as in the other commands.

## Exit statuses

| Status | Meaning | Output stream |
|---|---|---|
| 0 | Done, or already up to date; or `--status`; or `--check` with nothing to do | stdout |
| 1 | `--check` found a script pending or changed | stdout (the status listing) |
| 2 | The arguments are invalid (argparse's own status) | stderr |
| 3 | The database could not be reached within the connect wait (about 10 s) | stderr, one `error:` line |
| 4 | A migration did not complete: a lock wait timed out, or a script failed. Nothing from that script was applied or recorded; scripts before it stay applied. | stderr, one `error:` line |

No status prints a traceback. No output contains the connection's user or password. `credit-billing` (the wallet command) keeps 0, 1, 2, 3; 4 is new and belongs only to this command.

## Output

Applying (stdout):

```text
applied  20-credit-wallet.sql
current  22-billing.sql
1 applied, 1 already current.
```

`--status` / `--check` (stdout), one line per script, then a footer only when it applies:

```text
applied  20-credit-wallet.sql   2026-10-14T09:12:03Z
pending  22-billing.sql
changed  23-example.sql         recorded 1a2b3c4d…  shipped 9f8e7d6c…
unknown  99-from-a-newer-version.sql   recorded by another version; left alone
```

Footers: when the record table does not exist — `No migration record in this database yet. Scripts show as pending even where the schema already exists (a host-initialised database has it). Running this command is safe: the scripts are idempotent, and applying them is how an existing database is adopted.`

Errors (stderr, one line each, exit 3 and 4):

```text
error: cannot open a connection to db.internal:5432/appdata within 10s; check APPDATA_DATABASE_URL (host, port, database, credentials) and that the server is up
error: 20-credit-wallet.sql did not complete: could not get a lock within 10s (another transaction holds a table this script needs). Nothing from this script was applied; running again is safe.
error: 22-billing.sql did not complete: <the database's first message line>. Nothing from this script was applied.
```

Logs: JSON lines on stderr like the other commands (`billing_migrate_applied`, `billing_migrate_skipped`, with `script` and `checksum` as fields).

## Guarantees

- **Idempotent**: a second run applies nothing, and leaves every recorded time as it was.
- **Serialised**: any number of simultaneous runs against one database apply each script exactly once.
- **Atomic per script**: a script and its record commit together or not at all.
- **Bounded**: no wait lasts longer than `--lock-timeout-seconds` plus the connect wait; no wait is left to the database's deadlock detector.
- **Non-destructive on adoption**: re-applying an idempotent script to a database that already has it changes no row.
- **Read-only when asked**: `--status` and `--check` run no write statement.

## Not guaranteed

- It cannot make a non-idempotent script safe to edit in place (the package pins its own two as idempotent).
- It does not undo anything, and has no downgrade.
- It does not detect a database altered by hand away from what the scripts create; a `changed` checksum is about the script, not the live schema.
