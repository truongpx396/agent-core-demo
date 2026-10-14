# Implementation Plan: Billing Migrations and Idempotency Conflicts

**Branch**: `feat/billing-migration-tool` (PR A), `feat/billing-idempotency-conflict` (PR B) | **Date**: 2026-10-14 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/012-billing-migrate-idempotency/spec.md`

## Summary

Two gaps in the billing package, built in two PRs because the second needs the first.

**PR A — the migration tool.** `credit_billing/migrate.py` (library) and `credit_billing/cli/migrate.py` (`credit-billing-migrate`). It applies the shipped idempotent scripts in order, one transaction per script, under a transaction-level advisory lock taken before anything else (concurrent `CREATE TABLE IF NOT EXISTS` fails: research E4), with `lock_timeout` bounding every wait (research E1), and records `(script, checksum, applied_at)` in a ledger table it creates only when applying. Scripts are *repeatable* in Flyway's sense: a changed checksum means "apply again". `--status` and `--check` read without writing.

**PR B — the idempotency conflict.** One nullable `request JSONB` column on `credit_transactions`, added by a catalog-guarded block so a re-apply does not take `ACCESS EXCLUSIVE` on the hottest table (research E2). Every keyed write stores a normalised description of its request; a reused key is compared field by field over an explicit, validated set (default: all), and a difference raises `IdempotencyConflict` naming the fields. The two webhook callers narrow the set on purpose. The operator command reports a conflict as invalid input and gains an absolute `--expires-at`.

## Technical Context

**Language/Version**: Python 3.13 (the package declares `>=3.11`; nothing here needs 3.12+).

**Primary Dependencies**: no new ones. `psycopg[binary,pool]` (already a dependency), the standard library (`hashlib`, `re`, `argparse`, `dataclasses`, `json`). `psycopg.types.json.Jsonb` for the request column.

**Storage**: PostgreSQL 14+ (the shipped scripts need `CREATE OR REPLACE TRIGGER`). One new table (`billing_schema_migrations`, PR A) and one new nullable column (`credit_transactions.request`, PR B).

**Testing**: pytest from `packages/billing` (hermetic, `-m integration`, `-m provider_sandbox` untouched). Hermetic tests use a fake connection; integration tests use the package's own throwaway Postgres (`tests/containers.py`) and create extra databases inside it so they never touch the shared `appdata`.

**Target Platform**: Linux/macOS; CI runs the hermetic suite in a clean venv with only the package installed (`billing-package` job) and the integration tier in `test-integration`.

**Project Type**: library + operator CLI inside a monorepo package.

**Performance Goals**: none beyond bounded waits. `migrate` on an up-to-date database is two catalog reads and two ledger reads; it takes no table lock (it skips). The request column adds one small JSON value per keyed write.

**Constraints**: a wait never exceeds `--lock-timeout-seconds` (default 10 s) plus the connect wait; the package imports nothing from the app; the host's `postgres-init/20-` and `22-` stay byte-equal to the package's; no model-callable path to a balance.

**Scale/Scope**: 2 shipped scripts today; the design carries to tens. PR A is about 330 lines of source and 650 of tests; PR B about 220 and 600 (sizes below).

## Constitution Check

*GATE: passed before Phase 0 research; re-checked after Phase 1 design (below). Constitution v1.1.1.*

| Principle | Touched? | How it is satisfied |
|---|---|---|
| **I. Fail-closed tenant isolation** (NON-NEGOTIABLE) | PR B yes; PR A no | The ledger table is schema bookkeeping with no tenant data, so it carries no `tenant` column (stated, not assumed). The request column lives on `credit_transactions`, which already carries `tenant`; the uniqueness is `(tenant, idempotency_key)`, so a conflict is only ever found for the caller's own tenant and a conflict message can never quote another tenant's request. The compare reads the row by `WHERE tenant = %s AND idempotency_key = %s` (store-native scoping, as today). |
| **II. Mandatory human approval for side effects** (NON-NEGOTIABLE) | Both, structurally | No tool is added. `credit_billing.migrate` and the new CLI are not importable from any tool module: `tests/billing/test_the_wallet_is_not_reachable_by_the_model.py` forbids `credit_billing` in every module that serves a tool, and the new modules are inside it. The migration tool is an operator command, never a model action. |
| **III. Fixed, typed tools** | No | No tool added or changed. |
| **IV. Exactly-once side effects** (NON-NEGOTIABLE) | PR B directly; PR A by design | PR B *strengthens* the second layer: today `UNIQUE (tenant, idempotency_key)` makes a replay a no-op but cannot tell a replay from a different request; the stored request lets the wallet refuse the latter. The duplicate story is stated for every caller (table in `contracts/idempotency-request.md`): the charge path, the webhook grant, the webhook clawback, the operator command, the expiry. PR A: `migrate` is itself idempotent (a second run changes nothing), serialised across runners, and each script commits with its record or not at all. |
| **V. Bounded, observable failure** | Both | Every wait is bounded (connect 10 s, locks `--lock-timeout-seconds`), every failure is one `error:` line with a distinct exit status, nothing waits unboundedly. A conflict is logged (tenant, key, field names) and surfaces through each caller's existing counter or exit status; no new metric (spec D14, disclosed). |
| **VI. Untrusted content is data** | PR B | A conflict message echoes `reason` text. It is truncated for display and printed to an operator terminal or a log as text, never interpreted. Webhook-derived fields (`provider`, `external_ref`) are already stored and already shown by `show` through `display.printable`; the CLI formats the conflict through the same function. |
| **VII. Test discipline with real backends** | Both | Hermetic tests with a fake connection pin statement shape and ordering; the real-Postgres tier proves what the database does (concurrent runners, a busy table, a failing script, adoption, upgrade, the guarded column add). Each PR carries a mutation check on its new guards. |
| **VIII. Why-first documentation and honest gaps** | Both | One numbered pattern per PR in `packages/billing/docs/PATTERNS.md` (the package's own pattern file, per "Self-contained packages"), each stating the failure that motivated it; the design note's section 6 is updated; each PR names what it leaves open. |
| **Self-contained packages** | Both | `migrate.py` and the CLI import only `credit_billing` and third-party libraries; the connection arrives by injection (`credit_billing.db`); the existing boundary tests (`packages/billing/tests/test_boundary.py` and the host's `tests/billing/test_billing_core_boundary.py`) cover the new modules with no exemption. The host reaches nothing new: `app/billing_integration/port.py` re-exports `IdempotencyConflict` in PR B only. |

**Gate result**: no violation, no Complexity Tracking entry. **Post-design re-check**: unchanged; the one new judgement call is that the ledger has no tenant, recorded in the table above and in `data-model.md`.

## Project Structure

### Documentation (this feature)

```text
specs/012-billing-migrate-idempotency/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0: verified experiments E1-E4, prior art, decisions with alternatives
├── data-model.md        # Phase 1: the ledger table, the request column, script and request states
├── quickstart.md        # Phase 1: runnable validation, throwaway Postgres
├── contracts/
│   ├── credit-billing-migrate.md   # the command: arguments, output, exit statuses
│   ├── migrate-api.md              # the Python interface
│   └── idempotency-request.md      # field vocabulary, normalisation, compare semantics, caller table
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
packages/billing/
├── pyproject.toml                           # PR A: console script credit-billing-migrate
├── README.md                                # PR A, PR B: the command, the exit statuses, the conflict
├── docs/PATTERNS.md                         # PR A: pattern 67; PR B: pattern 68
├── docs/design-note.md, docs/port.md        # PR A: section 6 prerequisite 1 done; PR B: the conflict in the contract
├── src/credit_billing/
│   ├── migrate.py                           # PR A  NEW  ~230 lines: shipped(), statements(), status(), migrate(), errors
│   ├── schema.py                            # PR A: executable(name), numeric ordering; docstring no longer says "no tool"
│   ├── cli/migrate.py                       # PR A  NEW  ~110 lines: credit-billing-migrate
│   ├── credits.py                           # PR B: IdempotencyConflict, request vocabulary, _begin_transaction compare, compare= on the *_in writes
│   ├── port.py                              # PR B: re-export IdempotencyConflict, document it in the contract
│   ├── webhooks.py                          # PR B: the two narrowing call sites; quarantine an idempotency_conflict at once
│   ├── cli/credits.py                       # PR B: --expires-at (mutually exclusive with --expires-in-days), conflict message with the hint
│   └── sql/20-credit-wallet.sql             # PR A: header comment names the tool; PR B: the guarded request column
├── tests/
│   ├── test_migrate.py                      # PR A  NEW  hermetic: fake connection
│   ├── test_migrate_cli.py                  # PR A  NEW  hermetic: arguments, output, exit statuses
│   ├── test_schema.py                       # PR A: executable(), ordering
│   ├── test_idempotency_request.py          # PR B  NEW  hermetic: normalisation, vocabulary, compare, validation
│   ├── test_credits_cli.py                  # PR B: --expires-at, the conflict message
│   ├── fixtures/20-credit-wallet.before-request-column.sql   # PR B: the script as it was, for the upgrade test
│   └── integration/
│       ├── test_migrate_real_postgres.py    # PR A  NEW  runners, busy table, failing script, adoption, drift, keyspace
│       └── test_idempotency_real_postgres.py# PR B  NEW  each field x each operation, legacy rows, upgrade, webhook callers
app/billing_integration/port.py              # PR B: re-export IdempotencyConflict
postgres-init/20-credit-wallet.sql           # PR A, PR B: byte-equal copy of the package's
Makefile                                     # PR A: billing-migrate, billing-migrate-status
```

**Structure Decision**: one library module and one CLI module per capability, parallel to `inbox.py`/`cli/inbox_sweep.py`. The request vocabulary lives in `credits.py` next to the writes it describes (it is the wallet's own concept); the migration tool is its own module because it knows nothing about the wallet beyond the shipped script list.

## PR A — detailed design

### The module (`credit_billing/migrate.py`)

```text
LEDGER_TABLE = "billing_schema_migrations"
LOCK_KEYS = (0x63626D31, 1)        # pg_advisory_xact_lock(int4, int4): its own key space (documented as non-overlapping with the
                                    # bigint form the wallet uses for tenants), so no tenant name can ever collide with it
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0

shipped() -> list[Script]           # (name, checksum) for each script, in order; checksum = sha256 of the file text as shipped
statements(name) -> str             # schema.executable(name): the shipped text minus the psql `\connect` line
async status() -> Status            # read-only; never creates the ledger
async migrate(*, lock_timeout_seconds=...) -> Result
errors: MigrationError > MigrationLockTimeout, MigrationFailed
```

`migrate` is, per shipped script, in order:

```text
async with get_connection() as conn:                # the provider's transaction: commit on clean exit, rollback on exception
    SET LOCAL lock_timeout = '<ms>ms'               # bounds the advisory wait AND every DDL lock wait below (E1)
    SELECT pg_advisory_xact_lock(k1, k2)            # BEFORE anything else: concurrent CREATE TABLE IF NOT EXISTS errors (E4)
    CREATE TABLE IF NOT EXISTS billing_schema_migrations (...)
    row = SELECT checksum FROM billing_schema_migrations WHERE script = %s
    if row and row.checksum == shipped.checksum:    # in-lock re-check: a concurrent runner may have just applied it
        skip
    else:
        <the script, executed as one multi-statement string>      # psycopg simple-query protocol: no parameters
        INSERT ... ON CONFLICT (script) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = now()
```

- `LockNotAvailable` inside the block becomes `MigrationLockTimeout(script, seconds)`; any other `psycopg.Error` becomes `MigrationFailed(script, <first line of the database message>)`; both are raised from inside the `async with`, so the transaction rolls back and nothing is recorded for that script. Earlier scripts stay recorded (each was its own transaction).
- The script text is applied as shipped except for the leading `\connect` line (research E3 applied both scripts twice through psycopg this way). `executable()` removes exactly `\connect <name>` and **raises** on any other line that begins with a backslash, so a meta-command the tool does not understand can never be sent to the server as SQL or silently dropped.
- `status()` opens one connection, asks `to_regclass('billing_schema_migrations')`, and only if the table exists reads it. Pending = shipped and not recorded; changed = recorded with a different checksum; unknown = recorded and not shipped; applied = recorded and equal.
- Ordering: by the integer before the first `-`, then by name, so `100-` sorts after `22-` (`schema.scripts()` today sorts by name; this fixes the latent bug and a test pins it).

### The command (`credit-billing-migrate`)

```text
credit-billing-migrate [--status | --check] [--lock-timeout-seconds N]
```

Uses `wired_pool()` and `scheduled_job("agent-core-billing-migrate")` from `cli/_runtime.py`, so it gets the fast connect failure, the JSON log lines and the metric flush the other commands have. Exit statuses: 0 done or up to date; 1 `--check` found work; 3 database unreachable (`DatabaseUnavailable`, shared constant); 4 a migration did not complete (lock wait or failing script). Output and statuses are specified in `contracts/credit-billing-migrate.md`.

### Edits to existing files (PR A)

- `schema.py`: `executable(name)`, numeric ordering, the docstring that says "There is no migration tool" now points at `migrate.py`.
- `sql/20-credit-wallet.sql` and `22-billing.sql` header comments: "Applying it … apply by hand" now names `credit-billing-migrate` (comments only; the host copies updated in the same commit; the drift test keeps them equal).
- `pyproject.toml`: `credit-billing-migrate = "credit_billing.cli.migrate:main"`.
- `Makefile`: `billing-migrate` and `billing-migrate-status` targets (same style as `billing-inbox-sweep`).
- `docs/design-note.md` section 6 item 1 marked done; `README.md` "What it is not (yet)" no longer lists the migration tool; `docs/PATTERNS.md` pattern 67.

### Test matrix (PR A)

| Layer | Test | Proves |
|---|---|---|
| hermetic | `executable()` strips `\connect`, refuses other meta-commands, leaves `$$` bodies alone | FR-002 |
| hermetic | ordering by integer prefix (`22-` before `100-`) | the latent bug |
| hermetic | `shipped()` checksums equal sha256 of the file bytes the host also carries | FR-003, byte-equal copies |
| hermetic | fake connection: pending applies + upserts in the order lock → ledger → check → script → record | FR-005, FR-006 order |
| hermetic | fake connection: equal checksum skips, changed re-applies, `applied_at` untouched on skip | FR-004 |
| hermetic | `LockNotAvailable` → `MigrationLockTimeout` with the seconds; `Error` → `MigrationFailed`; neither carries the URL | FR-007, FR-012 |
| hermetic | `lock_timeout` value validated (> 0) and rendered as whole milliseconds | FR-007 |
| hermetic | status never executes a write; absent ledger → all pending | FR-008 |
| hermetic | CLI: argument exclusivity, output lines, exit 0/1/3/4, no traceback, no credentials | FR-009, FR-010, FR-012 |
| integration | blank database → both applied and recorded, schema present; second run applies 0, `applied_at` unchanged | FR-001, FR-004, SC-001 |
| integration | host-initialised database with existing wallet rows → recorded, every row unchanged | FR-011, SC-001 |
| integration | tampered recorded checksum → `changed`, `--check` non-zero, `migrate` re-applies and restores | FR-004, FR-009, SC-004 |
| integration | four `migrate()` at once on a blank database → each script applied once in total | FR-006, SC-002 |
| integration | another session holds a lock the script needs → `MigrationLockTimeout` within wait + 1 s; nothing recorded; after release it succeeds | FR-007, SC-003 |
| integration | a script that fails midway (a broken shipped copy injected through a patched `shipped`) → nothing recorded, earlier script still recorded, exit 4 | FR-005, FR-010 |
| integration | the advisory key does not block, and is not blocked by, a bigint tenant lock | the key-space claim |
| integration | `credit-billing-migrate` as a subprocess against a closed port → exit 3 in about ten seconds | SC-007 |
| mutation | 12 mutants: no lock; lock after ledger; no in-lock re-check; no `SET LOCAL`; script outside the transaction; record before script; swallowed script error; `status` writes; `check` ignores `changed`; unknown fails `check`; wrong exit status; credentials in a message | each must turn a test red |

### Size (PR A)

About 330 lines of source (`migrate.py` 230, `cli/migrate.py` 110, `schema.py` 20 net), about 650 of tests, 3 small config/doc edits, plus this feature's spec documents. Over the ~400-line aim, inside the ~1,000 ceiling counting tests. It cannot be split further without leaving a library with no command or a command with nothing to call; the commits are ordered so each reviews on its own (spec, source, tests, docs).

## PR B — design at the level of files and tasks

### The column

```sql
-- in 20-credit-wallet.sql, after the CREATE TABLE credit_transactions block
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('credit_transactions') AND attname = 'request' AND NOT attisdropped) THEN
        ALTER TABLE credit_transactions ADD COLUMN request JSONB;
    END IF;
END $$;
```

A plain `ADD COLUMN IF NOT EXISTS` takes `ACCESS EXCLUSIVE` on every re-apply even when the column exists (research E2: it blocked behind an open reader and gave up under a 500 ms `lock_timeout`; the guarded block ran in 0 ms). `credit_transactions` is written by every debit, so the guard is not optional. Nullable, no default, no backfill (the table is append-only by trigger; spec D10).

### The request

`credits.py` gains `IdempotencyConflict(ValueError)` (so the operator command's existing `except ValueError` still returns status 2), the per-operation field vocabulary, and the normalisation in `contracts/idempotency-request.md`. `_begin_transaction` takes `request: dict[str, str | None]` and `compare: frozenset[str] | None`: it inserts with the request, and on a replay selects `(id, request)` and compares the selected fields; equal → `(None, existing_id)` exactly as today; different → `IdempotencyConflict`; stored request `NULL` → today's duplicate. `grant_in`, `debit_in`, `clawback_in`, `adjust_in` accept `compare=` and forward it; an unknown field name raises `ValueError` before any statement runs.

### Callers (the table that decides what each compares)

| Caller | Key | Compares | Why |
|---|---|---|---|
| Host charge via the port | usage event id | all (default) | deterministic from the stored event row |
| Webhook grant | `{provider}:{event_id}` | all except `expires_at` | the expiry is `now + days` at processing time; a concurrent replay computes a later instant |
| Webhook clawback | `clawback:{provider}:{payment_ref}` | kind only (`frozenset()`) | the key names the payment; two events for one refund differ in reason and reclaimable amount and must stay a duplicate |
| Operator command | `--key` | all | a human reads the conflict; `--expires-at` makes an expiry retry-exact |
| Expiry sweep | `expire:{lot_id}` | all (`lot_id`) | nothing else varies |

### Edits and tasks at plan level

`credits.py` (vocabulary, `IdempotencyConflict`, `_begin_transaction`, `compare=` plumbing); `webhooks.py` (two `compare=` arguments with comments; catch `IdempotencyConflict` in `_apply` → quarantine `idempotency_conflict` at once); `cli/credits.py` (`--expires-at`, exclusive group, conflict message with the hint); `port.py` and `app/billing_integration/port.py` (re-export, docstring); `sql/20-credit-wallet.sql` and the host copy; fixtures: the script as it was before the column, for the upgrade test; `docs/PATTERNS.md` pattern 68, `docs/port.md`, `docs/data-model.md`, `README.md`. Tests: hermetic (normalisation, vocabulary, validation, compare, exception text, plumbing); integration (each field of each operation conflicts, an identical retry is a duplicate, legacy rows, the upgrade through `migrate()`, concurrent differing requests, the webhook clawback and grant callers, the operator command end to end); mutation (10 mutants: compare skipped, `kind` not compared, a field dropped from the vocabulary, `NULL` request raising, `compare=` not validated, webhook clawback strict, webhook grant comparing the expiry, conflict not quarantined, exception not a `ValueError`, request stored after the check).

### Size (PR B)

About 220 lines of source, 600 of tests, plus docs and the fixture. Same reasoning as PR A.

## Risks and how they are handled

| Risk | Handling |
|---|---|
| Re-applying a script on a busy production table blocks writers | `lock_timeout` bounds it and the run rolls back cleanly; the column add is guarded (E2); the scripts' other statements are the same ones pattern 60 already analysed. A re-apply happens only when the checksum changed or on first adoption. |
| A false conflict turns a working caller into failures | The compare set is explicit per caller and the two known divergent callers narrow it; the existing webhook, charge and operator suites run unchanged against the new default (SC-005 requires zero false conflicts); a test replays two refund events and a later-expiry grant. |
| The edit-in-place model is edited non-idempotently | The package pins both shipped scripts' idempotency in a test that applies each twice; D1 names the rule and its limit (immutable versioned migrations are the next step, not this one). |
| A newer package's records on an older package's database | `unknown` is reported and never fails the check (D6). |
| The host's init copy and the package's copy drift | `tests/core/test_billing_package_copies.py` already fails on one changed byte; both PRs edit the two copies together. |

## Complexity Tracking

No constitution violation to justify. The one structural cost worth naming: two mechanisms now apply the schema on the host (docker init on a fresh volume; the tool on an existing one). They apply the same bytes, and the tool's adoption of an init-created database is tested.
