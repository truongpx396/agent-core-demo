# Data Model: The Billing Package Owns Everything Billing

Two tables, shipped as `24-billing-usage-events.sql` and `25-billing-export-outbox.sql` (idempotent, applied by `credit-billing-migrate`).
Neither touches an existing table. The agent's `usage_events` loses its three credit columns **on fresh volumes only**; existing
databases keep them, unused.

## `billing_usage_events` (24)

| Column | Type | Constraint | Meaning |
|---|---|---|---|
| `tenant` | `TEXT` | `NOT NULL`, `CHECK (tenant <> '')`, in the primary key | whose usage |
| `event_id` | `TEXT` | `NOT NULL`, `CHECK (event_id <> '')`, in the primary key | the caller's id for this unit of usage; also the wallet debit's idempotency key and the provider's |
| `kind` | `TEXT` | `NOT NULL`, `CHECK (kind <> '')` | the caller's category (the agent: chat, followups, ...); not a closed set here |
| `occurred_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | when it happened (the transaction's `now()`, equal to the caller's own row) |
| `recorded_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | when this row was written |
| `cost_usd` | `NUMERIC(18,12)` | `CHECK (cost_usd IS NULL OR cost_usd >= 0)` | the caller's cost; NULL = unpriced (unknown, never 0) |
| `credits` | `NUMERIC(18,6)` | `CHECK (credits IS NULL OR credits >= 0)` | what it is worth at the rate in force; NULL = credits off or unpriced |
| `credits_per_usd`, `markup` | `NUMERIC(18,6)` | `CHECK (... IS NULL OR ... > 0)` | the rate used, stored so a later rate change never rewrites a past call |
| `model` | `TEXT` | nullable | what the caller metered (sent to a usage-billing provider) |
| `tokens` | `INTEGER` | `CHECK (tokens IS NULL OR tokens >= 0)` | idem |

- `PRIMARY KEY (tenant, event_id)` is the **only** unique constraint (a second one would make `ON CONFLICT` arbitration unreliable; the
  host's outbox hit exactly that, spec 010 T023). Table-level `CHECK (credits IS NULL OR (credits_per_usd IS NOT NULL AND markup IS NOT NULL))`.
- Index `(occurred_at)`: the only reads are by time (retention, reconciliation) and by key (the outbox's foreign key).
- **Append-only by trigger**: UPDATE always refused; DELETE refused unless the transaction sets `billing_usage_events.allow_delete = 'on'`
  (`set_config(..., true)` is local to the transaction).

## `billing_export_outbox` (25)

| Column | Type | Constraint |
|---|---|---|
| `provider` | `TEXT` | `NOT NULL`, `CHECK (provider <> '')` |
| `tenant`, `event_id` | `TEXT` | `NOT NULL`; `FOREIGN KEY (tenant, event_id) REFERENCES billing_usage_events (tenant, event_id)` |
| `status` | `TEXT` | `NOT NULL DEFAULT 'pending'`, `CHECK (status IN ('pending','sent','expired','failed'))` |
| `attempts` | `INTEGER` | `NOT NULL DEFAULT 0`, `CHECK (attempts >= 0)` |
| `next_attempt_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` |
| `last_error_class` | `TEXT` | a reason code or class name, never an exception's text |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` |
| `sent_at` | `TIMESTAMPTZ` | `CHECK ((status = 'sent') = (sent_at IS NOT NULL))` |

- `PRIMARY KEY (provider, tenant, event_id)`; a partial index `(next_attempt_at, created_at) WHERE status = 'pending'` is the whole of the worker's claim read.
- Trigger: what a row IS (`provider, tenant, event_id, created_at`) never changes; a finished row (`sent`, `expired`, `failed`) is terminal.
- The composite foreign key replaces the old tenant-match trigger: it forbids exporting an event under another tenant by construction.
- The foreign key also means an event with an outbox row cannot be deleted: the retention job clears finished (`sent`) rows with their events in one statement and keeps events whose export is unfinished.

## Value objects (Python)

`Usage(tenant, event_id, kind, cost_usd: Decimal | float | None, model: str | None = None, tokens: int | None = None)`;
`Recorded(status: recorded | duplicate | off | failed, credits: Decimal | None, debit: Applied | None)`;
`Admission(status: allowed | exhausted | unavailable | degraded, available: Decimal | None, reserved: Decimal | None)`;
`CreditSummary(balance: Balance, enforced: bool)`.
