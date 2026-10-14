# Research: Billing Migrations and Idempotency Conflicts

Everything below was either run against a real PostgreSQL 16 in this repository's own test container (E1-E4, 2026-10-14) or read from the vendor's current documentation (R1-R3, fetched the same day). Where a document is silent, that is said and the gap is closed by an experiment.

## Part 1 — Experiments (real PostgreSQL 16, `postgres:16` via the package's `tests/containers.py`)

Script: run from `packages/billing` with the package's own container helper, each experiment in its own throwaway database.

| # | Question | Result |
|---|---|---|
| **E1** | Does `lock_timeout` bound a wait for an *advisory* lock? | **Yes.** A second session's `SELECT pg_advisory_xact_lock(7, 7)` under `SET LOCAL lock_timeout = '500ms'` failed with `LockNotAvailable` (SQLSTATE `55P03`) after 0.51 s while the first session held the lock. The PostgreSQL 16 documentation page for the advisory functions never mentions `lock_timeout`; this experiment is what closes that gap. |
| **E2** | Does `ALTER TABLE … ADD COLUMN IF NOT EXISTS` take a strong lock when the column already exists, and does a catalog-guarded `DO` block avoid it? | **Yes, and yes.** With another session holding an open read (an `ACCESS SHARE` lock) on the table, the plain statement waited and gave up under `lock_timeout = 500ms`; the guarded block (`IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('t') AND attname = 'request' AND NOT attisdropped)`) ran in 0.00 s. The guarded block still adds the column when it is absent. This is the repository's pattern 60 observation, now measured for the exact statement PR B needs. |
| **E3** | Can both shipped scripts be applied through psycopg to a blank database once the `\connect` line is removed, and applied again? | **Yes.** Each script as one `conn.execute(text)` in its own transaction, twice in a row: no error; seven tables exist afterwards. No `psql`-only syntax other than `\connect` is in either script (a grep for lines beginning with a backslash finds exactly the two `\connect appdata` lines). |
| **E4** | Do concurrent `CREATE TABLE IF NOT EXISTS` statements race? | **Yes.** Eight connections creating the same table at once, 40 rounds: **216 errors** (`DuplicateTable`, `DuplicateObject`, `UniqueViolation`). `IF NOT EXISTS` is not a concurrency guard. This is why the advisory lock is taken *before* the ledger table is created, and why it also covers the shipped scripts (they are full of `CREATE TABLE IF NOT EXISTS`): two runners starting at once against a blank database would otherwise fail. |

## Part 2 — Prior art (vendor documentation)

**R1 — Stripe, idempotent requests** (`docs.stripe.com/api/idempotent_requests`). "The idempotency layer compares incoming parameters to those of the original request and errors if they're not the same to prevent accidental misuse." Keys are up to 255 characters; v1 keys may be pruned after 24 hours (this wallet keeps its keys for ever, which is stricter and right for a ledger). Stripe's rule is the strict one: the *whole* request is compared. That is this feature's default (D11). It is also why Stripe's guidance is to compute request parameters once and reuse them on a retry, which is what `--expires-at` gives an operator.

**R2 — Flyway, repeatable migrations** (`documentation.red-gate.com/flyway/…/repeatable-migrations`). Repeatable migrations "are re-applied to a database on migrate every time their checksum changes"; the history table stores each one's filename and checksum; "It is your responsibility to ensure the same repeatable migration can be applied multiple times", normally with `CREATE OR REPLACE`. That is exactly the model the shipped scripts already follow (idempotent, edited in place), so D1 adopts the semantics and the vocabulary rather than inventing a policy. Flyway applies repeatables *after* versioned migrations; this package has only repeatables today, and the ordering rule is stated for when versioned ones are added.

**R3 — PostgreSQL 16, advisory locks** (`postgresql.org/docs/16/functions-admin.html`). Advisory locks "can be identified either by a single 64-bit key value or two 32-bit key values (note that these two key spaces do not overlap)." The wallet already uses the single-bigint form (`pg_advisory_xact_lock(hashtextextended(tenant, 0))`), so the migration lock uses the two-integer form: no tenant name can ever collide with it. A test holds one and takes the other to prove the claim here.

## Part 3 — Decisions

### Migration tool

**Decision M1 — Build a small tool; do not adopt Alembic, Flyway, dbmate, sqitch or yoyo.** Rationale: the package must stay installable alone with its declared dependencies and "a few configs"; every adoption candidate either brings a runtime or a second language (Flyway, dbmate, sqitch), or a framework model (Alembic's revision graph and an `alembic.ini`/`env.py` per consumer) that a library shipping two idempotent scripts does not need. The tool is about 230 lines and the semantics are Flyway's repeatable migrations. *Alternatives considered*: Alembic (a revision DAG per consumer project is more machinery than the problem), Flyway/dbmate (non-Python binaries in a Python package), keeping "run psql once" (the status quo that this feature closes).

**Decision M2 — Repeatable scripts: a changed checksum means apply again.** Rationale: R2, and the repository already evolves this schema by editing idempotent scripts in place (pattern 60 is about re-applying them). *Alternatives considered*: immutable versioned migrations (the right shape once a change cannot be written idempotently or needs a data backfill; not built now, and the ledger table has the columns to allow it later); refusing on a changed checksum (would make every comment fix a deploy blocker).

**Decision M3 — One transaction per script containing the record, behind `pg_advisory_xact_lock(int4, int4)` taken first.** Rationale: E4 (a lock is required before anything else), R3 (own key space), PostgreSQL's transactional DDL (script and record commit together or not at all). A transaction-level lock cannot leak past a crash or a dropped connection; a session-level lock could. *Alternatives considered*: a session-level lock for the whole run (leaks if the process is killed mid-run until the connection dies; and holds the lock across scripts for no benefit), no lock plus retry on `DuplicateTable` (E4 shows the failure modes are three different exceptions, one of them a plain `UniqueViolation`).

**Decision M4 — `SET LOCAL lock_timeout` bounds every wait; default 10 s.** Rationale: E1, and the pattern-60 reason: a wait that ends by timeout is a clean rollback, whereas a wait that ends by the deadlock detector chooses a victim among unrelated sessions. *Alternatives considered*: `statement_timeout` (would also abort legitimately slow DDL on a large table), no timeout (the failure this feature exists to prevent).

**Decision M5 — Strip exactly `\connect <name>`; refuse any other leading-backslash line.** Rationale: E3. Removing every backslash line would silently drop a future meta-command (`\set`, `\if`) the tool cannot emulate; refusing makes the incompatibility loud. *Alternatives considered*: shipping a second, pure-SQL copy of each script (two sources of truth, and the host's byte-equal copy must be the `psql` one), a `psql` subprocess (an external binary the package cannot assume).

**Decision M6 — The checksum is sha256 of the script text as shipped (including `\connect`).** Rationale: it equals the checksum of the file the host also carries byte-for-byte, so an operator can compute it with `sha256sum` and compare. *Alternative*: checksum of the executed text (would change if the stripping rule changed).

**Decision M7 — A separate console script and a library function.** Rationale: parallel to `credit-billing-inbox-sweep`; the wallet command's parser is the wallet's; a service shell can call `migrate()` at start without a subprocess. Nothing calls it automatically (D9).

### Idempotency conflict

**Decision I1 — Store the normalised request as JSON in a nullable column; compare it field by field.** Rationale: R1 (parameters are compared) and the user-facing requirement that the error name the fields that differ, which a hash cannot do. Nullable and un-backfilled because `credit_transactions` is append-only by trigger and a legacy row's original request is unknowable. *Alternatives considered*: a hash column (cannot say what differs), reconstructing the request from the lot and entries (a join per replay, lossy for the pricing and the reason, and different for every kind), comparing the existing scalar columns only (they hold `reason`, `actor`, `usage_event_id` and the three pricing numbers, but not the amount, source, provider, reference or expiry, and the pricing columns are rounded on storage so an exact compare would raise false conflicts).

**Decision I2 — The compared set is an explicit, validated, per-call choice; the default is every field of the operation; the kind is always compared.** Rationale: the two in-repository callers whose request can differ between attempts for incidental reasons (the clawback keyed by the payment, whose amount depends on when the lot was read; the grant whose expiry is `now + days`) were found by reading `webhooks._apply`, and then reproduced: tests that reach the wallet's second layer on purpose fail with `failed` on the strict default. A global "compare everything" would turn working behaviour into failures; a global "compare nothing" is today's gap. *Alternatives considered*: per-field flags on every function (more parameters, same effect), a separate `fact` mode (two concepts where one set of names expresses both), excluding `expires_at` globally (would silently keep the first expiry when an operator retries with a different one, the exact failure being fixed).

**Decision I3 — `reason` and `actor` are compared in the default.** Rationale: R1 compares the whole request; and a different reason under a reused key means the audit text the operator just typed was not recorded, which "already applied" would hide. The clawback is where it must not be compared, and it narrows explicitly.

**Decision I4 — The exception subclasses `ValueError`.** Rationale: the operator command already turns `ValueError` into exit status 2 with the message; the port's callers already treat a `ValueError` from a write as a caller error. A subclass keeps both working and lets a caller catch the specific type.

**Decision I5 — A conflict in a webhook grant is quarantined at once.** Rationale: the webhook outcome table already defines `quarantined` as "retrying cannot fix it"; a conflict is that. Without the catch, `process_event` records a `failed` and retries up to `BILLING_WEBHOOK_MAX_ATTEMPTS` times before quarantining with a less useful reason.

**Decision I6 — No new metric.** Rationale: the conflict is visible through each caller's existing signal (the charge path's degraded counter and a `credit_debit_failed` log with `error_class=IdempotencyConflict`; the webhook `quarantined` outcome, which is already alerted; the operator command's exit status) and a structured log event. A counter needs an alert threshold, and there is no evidence yet for one. Disclosed in the spec.

## Part 4 — What this research does not establish

- Behaviour on PostgreSQL versions other than 16 (the scripts need 14+; the tests run 16).
- Throughput of the lock under many concurrent runners: the tool is run by a deploy, not by traffic; four concurrent runners are tested for correctness, not speed.
- Whether a managed-database role used in production can run the scripts' DDL: it is stated as an assumption, and a failure surfaces as `MigrationFailed` with the database's own message.
