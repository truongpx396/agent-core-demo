# Data Model: Billing Migrations and Idempotency Conflicts

Two additions to the package's schema, one per PR. Neither changes an existing column or constraint.

## PR A — `billing_schema_migrations` (created by the tool, not by a shipped script)

| Column | Type | Constraint | Meaning |
|---|---|---|---|
| `script` | `TEXT` | `PRIMARY KEY`, `CHECK (script <> '')` | the shipped script's file name, e.g. `20-credit-wallet.sql` |
| `checksum` | `TEXT` | `NOT NULL`, `CHECK (checksum <> '')` | sha256 (hex) of the script text as shipped, including its `\connect` line, at the time it was last applied |
| `applied_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | when the script was last applied (set again on a re-apply; **not** touched when a run skips the script) |

- **No `tenant` column, on purpose.** The table describes the schema's state, not any tenant's data (constitution I applies to tenant data). It is global to the database.
- **Created by the tool, inside the advisory lock**, with `CREATE TABLE IF NOT EXISTS`, so a database never has a record table the tool did not make. A read-only status never creates it.
- **Not append-only.** A re-apply updates the row (`ON CONFLICT (script) DO UPDATE`). The history of re-applies is not kept; if it is ever wanted it is a second table, not a change to this one.
- **No foreign keys** to or from any wallet table; dropping it loses only the memory of what was applied (the next run re-applies, which is safe).

### Script state (derived, never stored)

```text
                shipped?   recorded?   checksums equal?   state
                yes        no           —                  pending     -> migrate applies and records it
                yes        yes          yes                applied     -> migrate skips it
                yes        yes          no                 changed     -> migrate re-applies and re-records it
                no         yes          —                  unknown     -> reported, never touched, never fails --check
```

`pending → applied` on a successful run; `applied → changed` when the shipped script changes (a package upgrade, or a tampered record); `changed → applied` on the next run. A failed or timed-out script leaves its state exactly as it was.

## PR B — `credit_transactions.request`

| Column | Type | Constraint | Meaning |
|---|---|---|---|
| `request` | `JSONB` | nullable, no default | the normalised description of the request this transaction's idempotency key stood for, written once with the row |

- Added by a catalog-guarded `DO` block in `20-credit-wallet.sql` (plan, "The column"); a re-apply of the script does not take a strong lock on the table.
- **`NULL` means "written before this feature"** and a reused key on such a row behaves exactly as before: a duplicate, no comparison (FR-018). There is no backfill: the table is append-only by trigger, and a legacy row's original request is unknowable.
- **Tenant isolation is inherited**: the row already carries `tenant`, the uniqueness is `(tenant, idempotency_key)`, and the lookup that reads `request` is `WHERE tenant = %s AND idempotency_key = %s`.
- The value is JSON of strings and `null` only (amounts as fixed-point strings, instants as UTC ISO 8601), so it round-trips without loss and compares with plain equality. The exact keys per operation are in `contracts/idempotency-request.md`.

### Recorded request (what is stored)

| Operation | Keys stored |
|---|---|
| grant (including a positive adjustment) | `kind`, `amount`, `source`, `provider`, `external_ref`, `expires_at`, `reason`, `actor` |
| debit, clawback, negative adjustment | `kind`, `amount`, `usage_event_id`, `cost_usd`, `credits_per_usd`, `markup`, `reason`, `actor` |
| expiry | `kind`, `lot_id` |

All keys are always stored for an operation (absent values as `null`); *which of them are compared* is decided per call, not stored.

### Idempotency conflict (an error, not a row)

Carries: the key, the tenant, and for each differing field the first and the new value. Nothing is written when it is raised.

## What does not change

`credit_accounts`, `credit_lots`, `credit_entries`, every trigger and constraint, `22-billing.sql`, and the webhook inbox. `usage_events` (the agent app's meter) is untouched.
