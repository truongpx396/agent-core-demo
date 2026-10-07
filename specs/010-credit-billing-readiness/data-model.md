# Data Model: Credit-Based Billing Readiness

All tables live in the `appdata` database, are created by new numbered `postgres-init/NN-*.sql` scripts (constitution: schema
changes; existing volumes apply them by hand with `psql -f`), and carry their **own `tenant` column** (principle I: a child table
never inherits tenant only through a join). Every statement is parameterised and tenant-filtered inside the query.

Money and credits are `NUMERIC`, never float. `cost_usd` keeps the existing `NUMERIC(12,6)`; credits are `NUMERIC(18,6)`.

## Metering

### `usage_events` — one row per model call, append-only

| Column | Type | Notes |
|---|---|---|
| `event_id` | `TEXT PRIMARY KEY` | deterministic (UUIDv5 over tenant, thread, call identity); **the provider idempotency key** (≤100 chars) |
| `tenant`, `principal` | `TEXT NOT NULL` | |
| `thread_id` | `TEXT NOT NULL` | |
| `kind` | `TEXT NOT NULL` | closed set: `chat`, `followups`, `compaction`, `subagent`, `embedding`, `cron` |
| `model_alias`, `resolved_model` | `TEXT` | the alias the app used and the concrete model behind it (nullable, as in the ledger) |
| `input_tokens`, `output_tokens`, `cached_input_tokens`, `total_tokens` | `INTEGER NOT NULL DEFAULT 0` | |
| `cost_usd` | `NUMERIC(18,12)` | **NULL when unpriced** (never 0): unknown is not free. Twelve places because a cheap call costs a fraction of a millionth of a dollar and six would store it as zero |
| `price_input_per_token`, `price_output_per_token` | `NUMERIC(18,12)` | the price snapshot used |
| `credits` | `NUMERIC(18,6)` | credits charged; NULL when credits are off or the call is unpriced |
| `credits_per_usd`, `markup` | `NUMERIC(18,6)` | the rate in force, so a later rate change never rewrites history |
| `occurred_at` | `TIMESTAMPTZ NOT NULL` | when the call happened (also the provider timestamp) |
| `recorded_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

The credit columns above (`credits`, `credits_per_usd`, `markup`) are **not** in the first migration: PR 1a creates the table without them (`postgres-init/19-usage-events.sql`) and PR 3 adds them with `ALTER TABLE`, since nothing can charge credits before a wallet and a rate exist.

Indexes: `(tenant, occurred_at)`. **Invariant:** a trigger rejects `UPDATE` and `DELETE`, except through the retention job's role.
`INSERT … ON CONFLICT (event_id) DO NOTHING` is the duplicate story, and it relies on that primary key (constitution VII: an
integration-tier test proves it against a real Postgres, not a fake cursor).

## Wallet

### `credit_lots` — one row per grant

`id UUID PK`, `tenant`, `source` (`purchase`, `subscription`, `promo`, `manual`, `adjustment`, `overdraft`),
`provider` (nullable), `external_ref` (the provider's payment or event reference, nullable), `granted` `NUMERIC(18,6)`,
`expires_at` (nullable: credits do not expire unless set), `created_at`, `created_by`.

### `credit_transactions` — the idempotency unit

`id UUID PK`, `tenant`, `kind` (`grant`, `debit`, `expire`, `adjust`, `clawback`), `idempotency_key TEXT NOT NULL`,
`usage_event_id` (nullable, for a debit), `reason TEXT`, `actor TEXT NOT NULL`, `created_at`.
**`UNIQUE (tenant, idempotency_key)`**: replaying any grant, debit, expiry or clawback with the same key inserts nothing.

### `credit_entries` — signed amounts, append-only

`id BIGSERIAL PK`, `transaction_id FK`, `tenant`, `lot_id FK`, `amount NUMERIC(18,6) NOT NULL` (grants positive; debits, expiries and
clawbacks negative). A debit that spans lots is one transaction with several entries.

**Invariants** (each has a test):
1. `balance(tenant) = SUM(credit_entries.amount)`; nothing else is "the balance".
2. A lot's remaining amount is the sum of its entries and is never negative, except the tenant's `overdraft` lot.
3. Debits consume lots by earliest `expires_at` (NULLs last), then oldest `created_at`.
4. A debit larger than the available lots books the shortfall as a negative entry on the tenant's `overdraft` lot; the next grant first repays it.
5. A debit and the `usage_events` insert that caused it commit in **one transaction**; `credit_transactions.idempotency_key = event_id`.
6. Debits for one tenant serialize on `pg_advisory_xact_lock(hashtext(tenant))`. Throughput per tenant is low (a few model calls a second at most), so a per-tenant lock is simpler and safer than row-level juggling.
7. Entries are never updated or deleted. A mistake is corrected by an `adjust` transaction with an actor and a reason.

### Credit math

`credits = round_half_up(cost_usd × CREDITS_PER_USD × MARKUP, 6)`. An unpriced event debits nothing and is counted
(`agent_unpriced_usage_total`), so unknown is never silently free. `CREDITS_PER_USD` has no shipped default (open decision O1).

## Money in

### `billing_customers` — tenant ↔ provider customer

`tenant`, `provider`, `customer_ref`, `created_at`; **`UNIQUE (provider, customer_ref)`** and **`UNIQUE (tenant, provider)`**. Written by the
app when it creates the provider-side customer at checkout, never from a webhook. This is the only way a webhook reaches a tenant.

### `credit_products` — the server-side catalog

`provider`, `product_ref`, `credits NUMERIC(18,6)`, `expires_after_days` (nullable), `active`. A purchase's value is read from here.

### `billing_webhook_events` — the inbox

`provider`, `event_id`, `event_type`, `received_at`, `status`, `attempts`, `tenant` (nullable until linked), `payload JSONB` (scrubbed), `error_class`.
**`UNIQUE (provider, event_id)`**.

```
received ──▶ applied            (a grant, clawback or no-op was committed in the same transaction as this status)
   │  └────▶ ignored            (a type we do not act on; recorded so it is not re-fetched)
   ├───────▶ quarantined        (no tenant link, unknown product, or an out-of-order event past its deadline; alerted, grants nothing)
   └───────▶ failed ──▶ received  (bounded retries, then quarantined)
```

## Money out

### `usage_export_outbox`

`event_id FK`, `tenant`, `provider`, `status`, `attempts`, `next_attempt_at`, `last_error_class`, `created_at`, `sent_at`; `PRIMARY KEY (provider, event_id)`.
Created in the same transaction as the event, **only** for a tenant linked to a provider that declares `USAGE_EXPORT`.

```
pending ──▶ sent
   │  └───▶ pending      (retry: exponential backoff, capped; attempts bounded)
   └──────▶ expired      (age ≥ 30 days, inside Stripe's 35-day window: counted and alerted, never silently dropped)
```

A worker claims rows with `FOR UPDATE SKIP LOCKED`, so replicas never double-send; the provider-side idempotency key (the event id) is
the second layer, because a crash between "sent" and "marked sent" is the one window the first layer cannot see.

## Metrics (each degrade path counted; alerts for the paths that hide money)

`usage_event_write_failed_total` (**alert**: a lost event is lost revenue), `usage_event_unpriced_total`, `credit_debit_overdraft_total`,
`credit_enforcement_refused_total`, `billing_webhook_total{provider,outcome}` (`applied|duplicate|ignored|quarantined|invalid_signature|failed`),
`billing_webhook_quarantined` (**alert**), `usage_export_total{provider,outcome}`, `usage_export_oldest_pending_age_seconds` (**alert** at 7 days),
`usage_export_expired_total` (**alert**), `credit_reconcile_max_drift_usd` (a gauge: the largest per-tenant drift in the last run, so no tenant label; the per-tenant detail is in the report; **alert** above a threshold).
