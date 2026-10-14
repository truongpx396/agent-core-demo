# Feature Specification: Billing Migrations and Idempotency Conflicts

**Feature Branch**: `012-billing-migrate-idempotency`

**Created**: 2026-10-14

**Status**: Proposed. Nothing in this spec is built yet; `plan.md` fixes the two-PR sequence.

**Input**: "Close the two gaps spec 011 left open in the billing package, so the package can be operated against an existing database and used by callers that retry. (1) A migration tool. (2) An idempotency conflict: a reused idempotency key with a different request must be refused, not silently answered with the first result."

> **Boundaries.** This builds on feature 011 (the billing package `packages/billing/`, import name `credit_billing`, distribution
> `credit-billing`). Both gaps were disclosed there: the design note (`packages/billing/docs/design-note.md`, section 6) names the
> migration tool as "the first prerequisite … independent of everything else", and PR C's body listed "a reused idempotency key with
> different parameters returns the first result". This feature changes **what the package can do for the people and programs that
> run it**; it does not move code between the package and the agent app, and it keeps the rule that the package imports nothing from the app.
>
> **Not in this feature** (the rest of the former roadmap items 012-014, renumbered by this feature to 013 and later): holds
> (reserve/capture/release), the service-side event store, outbound notifications, an HTTP service shell, service authentication,
> and turnkey catalog configuration.

## Why this is shaped the way it is

Two facts in the repository decide the shape, and they connect.

1. **The shipped schema can only be applied by `psql`, and only once.** `20-credit-wallet.sql` and `22-billing.sql` are idempotent
   (`CREATE … IF NOT EXISTS`) but open with `\connect appdata`, a `psql` meta-command, so no driver can run them as they are. They run
   automatically on a fresh database volume, or an operator runs them by hand "once" against an existing one. There is no record of
   what was applied, no way to ask "is this database current?", and no protection when two deploys run at once. That is tolerable for
   one app on one laptop and wrong for a service that deploys on its own cadence against a database that already holds money.
2. **A reused idempotency key is answered with the first result, whatever was asked.** The wallet's `UNIQUE (tenant, idempotency_key)`
   makes a replay insert nothing, which is exactly right for a retry and exactly wrong for a *different* request that happens to reuse
   the key: a caller that asks for 600 credits under a key that already granted 500 is told "already applied", and the 600 is lost
   without a trace. For a financial record that is the worst failure mode: it looks like success.

Fixing (2) needs one new column on `credit_transactions` (the request the key stood for). Getting that column onto a database that already
has data is precisely what (1) is for. That is why the migration tool comes first and the conflict check is built on it.

The subtle part of (2) is **what counts as "the same request"**. Two callers in this repository legitimately differ between attempts, and
a naive "compare everything" would turn working behaviour into failures:

- A webhook **clawback** is keyed by the *payment* (`clawback:{provider}:{payment_ref}`). Two different provider events can describe the
  same refund, and the amount each asks for is what the lot could still give back *when that event read it* (a debit can land between two
  reads), so the second can ask for a different amount; the designed outcome is still that it is a harmless duplicate ("a payment is taken
  back at most once").
- A webhook **grant** computes its expiry as *now + the product's days* at processing time, so any replay computes a later instant. The
  earlier layers (the inbox claim, and the payment's lot check) normally answer a replay before the wallet is reached, so a replay is rare
  here and this layer is defence in depth. A defence layer must not fail on its own, and comparing that instant would turn a harmless
  replay into a failure.

Reading the code showed this is narrower than it first looks, so the claim was tested rather than assumed: tests that make the earlier layers
miss, on purpose, fail with `failed` on the strict default and pass with the narrowing.

So which fields identify a request must be an **explicit, validated choice per call**, with the strict full comparison as the default,
and each of those two callers must say precisely what it compares and why.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An operator brings any database up to date, safely, and can gate a deploy on it (Priority: P1) — PR A

An operator (or a deploy pipeline) has a database that already holds wallets and ledger entries, or one the agent app initialised from
its own init scripts, or an empty one. They run one command and the package's schema is applied in order; they can ask, without
changing anything, what is applied and what is not; and a pipeline can refuse to start the application against a database that is behind.

**Why this priority**: it is independent of everything else, it unblocks running billing as a service, and the idempotency conflict
(Story 2) cannot reach an existing database without it.

**Independent Test**: against a throwaway Postgres, run the tool against (a) an empty database, (b) a database initialised by the
shipped scripts as the host does, (c) a database whose recorded checksum no longer matches the script. After each, a second run changes nothing and a
status run reports everything applied; the "check" form exits non-zero before and zero after.

**Acceptance Scenarios**:

1. **Given** an empty database, **When** the operator runs the tool, **Then** both shipped scripts are applied in order, each is recorded
   with its name, checksum and time, and a second run applies nothing.
2. **Given** a database the host's init scripts created (the schema exists, nothing is recorded), **When** the tool runs, **Then** it
   records both scripts without damaging the existing data (re-applying an idempotent script is the adoption) and every wallet and ledger row is unchanged.
3. **Given** two copies of the tool started at the same moment, **When** both run, **Then** each script is applied exactly once and both exit successfully.
4. **Given** another session holds a lock on a table a script needs, **When** the tool runs, **Then** it stops within the configured wait
   with a message that names the cause and says re-running is safe, and nothing from that script is recorded.
5. **Given** a script that fails partway, **When** the tool runs, **Then** the script is rolled back whole, nothing is recorded for it,
   and the exit status says the migration did not complete.
6. **Given** a pending or changed script, **When** the operator runs the read-only status, **Then** it lists each script as applied,
   pending or changed and changes nothing (not even creating the record table); **and** the "check" form exits non-zero.
7. **Given** the database is unreachable, **When** the tool runs, **Then** it fails within seconds with one `error:` line and exit status 3, as the other operator commands do.

---

### User Story 2 - A caller that retries is protected from a silently lost request (Priority: P1) — PR B

A program (the agent app's charge path, a webhook handler, an operator at a terminal, later a remote client) sends a wallet request under
an idempotency key. If it sends the *same* request again, it must be told "already done". If it sends a *different* request under the same
key, it must be refused with an error that says what differs, because answering "already done" would hide that the second request was never applied.

**Why this priority**: it is the correctness hole in a financial record, and the precondition for exposing the wallet to callers that
are not under this repository's control.

**Independent Test**: grant 500 credits under key K; grant 500 again under K (a duplicate, nothing changes); grant 600 under K (refused, naming
`amount`, nothing changes); grant 500 with a different reason under K (refused, naming `reason`). Then replay two different refund events for
one payment, and one webhook grant with a later expiry: both stay harmless duplicates.

**Acceptance Scenarios**:

1. **Given** a grant applied under key K, **When** the identical request is sent again, **Then** the result is a duplicate pointing at the original transaction and nothing changes.
2. **Given** a grant applied under key K, **When** a request with a different amount, source, expiry, provider, reference or reason is sent
   under K, **Then** it is refused with an error naming exactly the fields that differ, and the ledger, the lots and the balance are unchanged.
3. **Given** a key used for a grant, **When** the same key is used for a debit, **Then** it is refused, whatever else matches (the kind is always compared).
4. **Given** two different provider events describing one refund, **When** both are processed, **Then** the first takes the credits back and the second is
   still reported as already reversed (a duplicate), although their reason and reclaimable amount differ.
5. **Given** a webhook grant that reaches the wallet a second time (the inbox row was swept and the payment's lot check missed it), **When** the second attempt computes a later expiry, **Then** it is still a duplicate, not a conflict.
6. **Given** a webhook grant whose catalogue amount changed between two attempts under one provider event, **When** the second attempt runs, **Then** it
   is quarantined at once with a reason that says so (retrying cannot fix it), and the customer's first grant stands.
7. **Given** a transaction written before this feature (no recorded request), **When** its key is reused, **Then** the behaviour is exactly today's: a duplicate.
8. **Given** a caller that names a field to compare that does not exist, **When** it calls, **Then** the call is refused before anything is written.

---

### User Story 3 - The operator command makes a safe retry possible and a conflict understandable (Priority: P2) — PR B

An operator who ran `credit-billing grant … --key K` and is unsure it landed runs it again with the same key. If it landed, they are told so. If
what they typed now differs from what landed, they are told *which fields* differ and how to retry exactly. Because `--expires-in-days` is
recomputed on every run, they can also give an absolute `--expires-at`, which retries identically.

**Why this priority**: the operator command is the one caller where a human reads the conflict, so the message is the feature; it is smaller than Story 2 and depends on it.

**Independent Test**: grant with `--key K --expires-in-days 30`; repeat the identical command a minute later (an expiry conflict, naming the old instant); repeat with
`--expires-at <that instant>` (a duplicate).

**Acceptance Scenarios**:

1. **Given** a landed grant, **When** the same command is repeated with a different `--amount` under the same `--key`, **Then** the command exits with the invalid-input status and one `error:` line naming `amount`.
2. **Given** a landed grant made with `--expires-in-days`, **When** it is repeated, **Then** the conflict names `expires_at` with both instants and suggests `--expires-at` with the first one.
3. **Given** `--expires-at` and `--expires-in-days` together, **When** the command runs, **Then** it is refused as invalid input before touching the database.

---

### Edge Cases

- A script is edited after it was recorded (a comment fix, an added `IF NOT EXISTS` column): the tool sees a changed checksum, re-applies the idempotent script, and records the new checksum. A script that is **not** idempotent must never be edited in place; the package's tests pin the shipped scripts' idempotency.
- The record table does not exist yet and the tool is only asked for status: it reports every script as pending and creates nothing.
- The record names a script this version of the package does not ship (a newer version ran against this database): it is listed as unknown and left alone; it is not an error and not a reason to fail the check.
- The tool runs with a database role that cannot create tables: the failure is reported as a failed migration with the database's own message, nothing is recorded.
- Two requests under one key arrive at once with different content: exactly one wins, the other gets the conflict (the wallet already serialises per tenant).
- A key reused across tenants is two different keys (uniqueness is per tenant); a conflict is never reported across tenants and never reveals another tenant's request (Principle I).
- A debit of zero credits records nothing, so a key used for it can be reused later for a real debit; this is unchanged.
- A very long reason in a conflict message is truncated for display; what is stored is not.
- The recorded request of a transaction is never updated (the ledger is append-only); a legacy transaction stays legacy for ever.

## Requirements *(mandatory)*

### Functional Requirements

**The migration tool (PR A)**

- **FR-001**: The package MUST apply its shipped schema scripts, in their numeric order, to the database named by the operator's connection setting, through a command (`credit-billing-migrate`) and through a Python interface that uses the package's injected connection.
- **FR-002**: The tool MUST remove psql-only meta-commands (the leading `\connect`) from a script before applying it, and MUST NOT change the shipped files, so the host's byte-equal copies stay valid init scripts.
- **FR-003**: The tool MUST record, per script, its name, a checksum of its content and the time it was applied, in a table it owns, and MUST create that table only when applying, never when reporting.
- **FR-004**: A script whose recorded checksum differs from the shipped one MUST be re-applied and re-recorded; a script that is not recorded MUST be applied and recorded. Running again with nothing to do MUST change nothing, including the recorded time.
- **FR-005**: Each script MUST be applied in its own transaction together with its record, so a failure leaves neither the script's changes nor its record.
- **FR-006**: Concurrent runs MUST serialise so each script is applied once, and a run that finds another has already applied a script MUST skip it.
- **FR-007**: A run MUST give up, after a configurable wait (default ten seconds), when it cannot get the locks a script needs, with an error that says what was waited for and that re-running is safe; it MUST NOT wait unboundedly and MUST NOT depend on the database's deadlock detector to resolve a wait.
- **FR-008**: A read-only status MUST list every shipped script as applied, pending or changed (with the recorded and shipped checksums), and every recorded script the package does not ship as unknown; it MUST NOT write.
- **FR-009**: A check form MUST exit non-zero if and only if some shipped script is pending or changed (unknown scripts do not fail it), writing nothing.
- **FR-010**: The command MUST use exit status 0 for success or up-to-date, 1 for a check that found work to do, 3 when the database cannot be reached within the shared connect wait (the same status and message shape as the other operator commands), and 4 when a migration could not complete (a lock wait that timed out, or a failing script), each with one `error:` line and no traceback.
- **FR-011**: Adopting a database that the host initialised MUST leave every existing row unchanged and MUST NOT require the operator to say it was initialised by another means.
- **FR-012**: The error messages and the status output MUST NOT contain the connection's user or password.

**The idempotency conflict (PR B)**

- **FR-013**: Every wallet write that carries an idempotency key (grant, debit, clawback, adjustment, expiry) MUST record, in the same row as its transaction, a normalised description of the request the key stood for.
- **FR-014**: A reused key whose recorded request equals the new one (under the fields chosen for comparison) MUST return a duplicate that points at the original transaction and changes nothing.
- **FR-015**: A reused key whose request differs MUST raise a conflict error that names the differing fields with the first and the new value, changes nothing, and is logged with the tenant, the key and the field names.
- **FR-016**: The strict comparison (kind, amount, source, provider, external reference, expiry, usage event, pricing, reason, actor, as applicable to the operation) MUST be the default.
- **FR-017**: A caller MAY narrow the comparison by naming the fields to compare; the kind MUST always be compared; an unknown field name MUST be refused before any write; the narrowing MUST be explicit at the call site and documented there.
- **FR-018**: A transaction with no recorded request (written before this feature) MUST behave exactly as today when its key is reused.
- **FR-019**: A webhook clawback MUST compare only the kind (the key names the payment), so two events for one refund remain a duplicate; a webhook grant MUST compare everything except the expiry instant; a conflict in a webhook grant MUST quarantine the event at once with a stable reason, not retry it.
- **FR-020**: The operator command MUST report a conflict as invalid input (the existing status 2), print the fields that differ, and for an expiry difference suggest the retry-safe absolute form; it MUST accept an absolute `--expires-at` and refuse it combined with `--expires-in-days`.
- **FR-021**: The new column MUST reach a database that already has data through the migration tool, and a fresh database through the shipped script; the host's copies of the script MUST be updated and remain byte-equal.
- **FR-022**: The port contract and its in-process implementation MUST document and pass through the conflict error so a future remote implementation can map it to a conflict response.

**Both**

- **FR-023**: Tenant isolation, the absence of any model-callable path to a balance, and exactly-once effects (constitution I, II, IV) MUST be unchanged; the package MUST still import nothing from the application, and the existing boundary tests MUST pass without exemptions.
- **FR-024**: Each PR MUST carry regression tests that fail without its change, a mutation check that proves the new guards can fail, a pattern entry in the package's pattern file, and a statement of what is not done.

### Key Entities

- **Applied script record**: one row per shipped script that has been applied to a database: the script's name, the checksum of the content that was applied, and when. It describes the schema's state, not any tenant's data, so it carries no tenant.
- **Recorded request**: the normalised description of what an idempotency key stood for, stored with its transaction: operation kind, amount, and the fields that apply to the operation (source, provider, reference, expiry, usage event, pricing, reason, actor). Absent on transactions written before this feature.
- **Idempotency conflict**: the refusal raised when a key is reused for a different request, carrying the key, the tenant and the differing fields with their first and new values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator brings an empty, a host-initialised, and a drifted database to the current schema with one command each, and a second run on each changes nothing (zero scripts applied, recorded times unchanged).
- **SC-002**: Four copies of the tool started together against one database apply each script exactly once in 100% of runs (the integration test starts them concurrently and counts applications).
- **SC-003**: With a table locked by another session, the tool stops within the configured wait plus one second, names the cause, records nothing for that script, and a re-run after the lock is released succeeds.
- **SC-004**: The check form's exit status is non-zero exactly in the pending and changed cases and zero otherwise, across every state combination of the two scripts.
- **SC-005**: Every reuse of a key with a different request is refused naming the differing fields (checked over each field of each operation), and every identical retry is a duplicate; the number of false conflicts across the existing webhook, charge and operator-command test suites is zero.
- **SC-006**: A database that existed before the change gains the new column through the tool, keeps every existing transaction, and replays of those transactions' keys behave as before.
- **SC-007**: An unreachable database makes the new command exit with status 3 within twelve seconds with no traceback and no credential in the output.

## Assumptions and Decisions

Decisions were made here, with reasons, so the plan does not re-open them; the user can overturn any of them in review.

- **D1. The shipped scripts stay idempotent and may be edited in place; the tool treats them as *repeatable*.** This is how the repository evolves its schema today (and pattern 60 is about re-applying them safely). A changed checksum means "apply again", not "refuse". The alternative, immutable numbered migrations, is the right shape for a future service with data backfills and is **not** built now; the tool's record table does not preclude it. Disclosed in the pattern entry.
- **D2. One transaction per script, with the record in it, under a transaction-level advisory lock taken before anything else.** The lock serialises runners; the in-lock re-check makes the second runner skip; the record insert commits with the script or not at all.
- **D3. `lock_timeout` bounds every wait inside a script, default 10 s, configurable.** A wait that ends is a clean rollback; this also keeps the tool from ever being the session the deadlock detector picks (pattern 60).
- **D4. Adoption is re-application.** No special flag, no "baseline" command: the scripts are idempotent, so applying one to a database that already has it is a no-op that gets recorded.
- **D5. Status never writes.** If the record table is absent every script is "pending"; the output says the schema may already exist and that applying is safe.
- **D6. Unknown recorded scripts are reported and left alone, and do not fail the check.** A rollback to an older package version must not be blocked by a newer version's records.
- **D7. Exit statuses**: `credit-billing-migrate` uses 0, 1 (check found work), 3 (database unreachable; shared with the other commands), 4 (could not complete). The wallet command keeps 0, 1, 2 (invalid input, which now includes a conflict) and 3.
- **D8. The command is a separate console script, `credit-billing-migrate`**, parallel to `credit-billing-inbox-sweep`, not a subcommand of `credit-billing`: the existing parser is the wallet's and a migration is a different kind of command run by a different role.
- **D9. Nothing runs the tool automatically.** Not on import, not on application start, not in the Docker image. Applying schema is an operator or pipeline step. (A service shell may choose to run it at start; that is a decision for that feature.)
- **D10. The request is stored as JSON in a nullable column on `credit_transactions`, written once.** JSON, not a hash, so a conflict can say *which* field differs; nullable so legacy rows keep today's behaviour; no backfill, because the table is append-only by trigger and a backfill would defeat the guarantee.
- **D11. Strict by default; `reason` and `actor` are compared.** A different reason under a reused key means a different intent, and silently keeping the first reason would mislead an operator into believing their corrected reason was recorded. The strictness is what Stripe-style idempotency does.
- **D12. Narrowing is by naming fields, validated.** `compare=` takes a set of field names drawn from a fixed vocabulary; the kind is always compared. The two webhook call sites are the only narrowing users, each with a comment saying why.
- **D13. A webhook grant conflict is quarantined at once, not retried.** The webhook outcome table already says a quarantined event is one "retrying cannot fix"; a conflict is that.
- **D14. No new metric.** A conflict surfaces through each caller's existing counter (the charge path's degraded counter, the webhook outcome counter, the operator's exit status) plus a structured log event; adding a counter would need an alert decision that has no evidence behind it yet. Disclosed.
- **D15. Two PRs.** PR A: this spec, the migration tool, its tests and docs. PR B: the conflict, using the tool for the column. The ~400-line guideline applies to each, and the plan states each one's size.
- **D16. Renumbering.** This feature takes number 012. The former roadmap items 012-014 (holds and event store, service shell, turnkey) become 013 and later; PR A updates the design note's section 6 to say which prerequisites are now done.

**Assumptions**: the database is PostgreSQL 14 or later (the shipped scripts already require it for `CREATE OR REPLACE TRIGGER`; the tests run 16); the role that runs the tool may create tables and run the scripts' DDL; operators have the package installed (`pip install credit-billing`) or run it from the monorepo with the Makefile's path; the host keeps initialising fresh volumes from its byte-equal `postgres-init/` copies and runs the tool for databases that already exist.

### Questions for the plan

None are open. The plan resolves only the mechanics (the module layout, the exact SQL, the test shapes).
