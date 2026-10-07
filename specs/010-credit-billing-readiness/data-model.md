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

The credit columns above (`credits`, `credits_per_usd`, `markup`) are **not** in the first migration: PR 1a creates the table without them (`postgres-init/19-usage-events.sql`) and PR 3 adds them with `ALTER TABLE` (`postgres-init/21-usage-event-credits.sql`, re-runnable), since nothing can charge credits before a wallet and a rate exist.

**What `credits` means (decided in PR 3).** It is what the call is *worth* at the rate in force, whether or not the tenant has a wallet to charge: the same figure a usage-billing provider is sent. Whether a wallet was actually debited is the `credit_transactions` row whose `usage_event_id` and `idempotency_key` are this event's id. `credits` is NULL when credits are off (no rate) or the call is unpriced, and a real `0` for a free call; the database refuses credits without a rate (`usage_events_credits_have_a_rate`). The cost is rounded to the column's twelve places **before** it is multiplied, and that stored cost is what is multiplied, so `credits = credits_for_cost(cost_usd, credits_per_usd, markup)` holds for the row exactly and a reconciliation can recompute it. With no rate the credit columns are not mentioned at all (the original statement runs), so a deployment that has not applied the migration is unaffected.

Indexes: `(tenant, occurred_at)`. **Invariant:** a trigger rejects `UPDATE` and `DELETE`, except through the retention job's role.
`INSERT … ON CONFLICT (event_id) DO NOTHING` is the duplicate story, and it relies on that primary key (constitution VII: an
integration-tier test proves it against a real Postgres, not a fake cursor).

## Wallet

*(Built in PR 2: `postgres-init/20-credit-wallet.sql`, `app/billing/credits.py`.)*

### `credit_accounts` — opt-in

`tenant PK`, `created_at`, `created_by`. **A tenant is on credit billing if and only if it has a row here.** A tenant with no account is
never debited and never gated, so shipping the wallet changes nothing for an existing tenant until an operator, or a verified purchase
webhook, opens one (a grant opens it).

### `credit_lots` — one row per grant

`id UUID PK`, `tenant` (FK to the account), `source` (`purchase`, `subscription`, `promo`, `manual`, `adjustment`, `overdraft`),
`provider` / `external_ref` (nullable), `granted NUMERIC(18,6)`, **`remaining NUMERIC(18,6)`** (a cache, see invariant 2), `expires_at`
(nullable), `created_at`, `created_by`. `UNIQUE (id, tenant)` so children can reference both. A trigger allows **only `remaining` to change** and
refuses any delete. A partial unique index allows **at most one `overdraft` lot per tenant**.

### `credit_transactions` — the idempotency unit

`id UUID PK`, `tenant`, `kind` (`grant`, `debit`, `expire`, `adjust`, `clawback`), `idempotency_key TEXT NOT NULL CHECK (<> '')`,
`usage_event_id` (a plain reference, **not** a foreign key: events are trimmed by retention and the wallet is a financial record that must
outlive them), `reason`, `actor NOT NULL`, `created_at`. **`UNIQUE (tenant, idempotency_key)`**: replaying any grant, debit or expiry with
the same key inserts nothing. Append-only by trigger.

### `credit_entries` — signed amounts, append-only

`id BIGSERIAL PK`, `transaction_id`, `tenant`, `lot_id`, `amount NUMERIC(18,6) CHECK (<> 0)`. **Composite foreign keys**
`(transaction_id, tenant)` and `(lot_id, tenant)` mean an entry cannot reference another tenant's lot or transaction: a cross-tenant move
fails in the database, not only in code that might share the bug. Append-only by trigger.

**Invariants** (each has a test, and each guard has a mutant that fails it):
1. The ledger is the truth: `credit_entries` is never updated or deleted; a mistake is corrected by a new transaction with an actor and a reason.
2. A lot's `remaining` is a cache kept equal to the sum of its entries inside the lock, so a debit costs O(lots), not O(every debit ever made).
   `credits.verify()` reports any drift. No lot is negative except the overdraft lot.
3. Debits consume lots by earliest `expires_at` (NULLs last), then oldest `created_at`. A lot past `expires_at` is **never consumed**, whether or not
   the sweep has booked its expiry, so the available balance is right the instant it expires. `available` (live lots, minus debt) is distinct from
   `ledger` (every lot, including expired-unswept), as in Stripe's balance summary.
4. A debit larger than the available lots **is never refused** (the model call it pays for has already happened): the shortfall is booked on the
   overdraft lot (a negative `remaining`), counted (`agent_credit_overdraft_total`), and repaid first by the next grant. Stopping the spend is the job
   of gating, checked before the call.
5. A debit takes `idempotency_key = usage event id` and runs in the **caller's transaction**, so it commits together with the event that caused it. **Refinement (PR 3, spec D11):** it runs inside a *savepoint*, so a wallet fault rolls back only the debit and the event is kept, counted and alerted (`credit_debit`): the meter outranks the charge, because a lost event cannot be repaired and an uncharged one can.
6. Every write for a tenant takes `pg_advisory_xact_lock(hashtextextended(tenant))`. The row locks already stop a lost update on a lot; what only the lock
   adds is the **canonical state**: a debit racing a grant ends as a serial run would, never leaving a debt next to live credit. Removing the lock fails
   the stress test 3 of 3 times and no other test, which is how its purpose was established rather than assumed.
7. `expire_due(limit, tenant=None)` is a bounded, idempotent sweep, one transaction per lot, keyed `expire:<lot id>`.

*Deferred to the PRs that need them:* `adjust` (the operator CLI, PR 6) and `clawback` (refunds, PR 4), both thin variants of the same debit path. *(Both built. `adjust_in` in PR 6: positive is a grant whose lot source is `adjustment`, negative is a debit of kind `adjust`.)*

### Credit math

`credits = round_half_up(cost_usd × CREDITS_PER_USD × MARKUP, 6)`. An unpriced event debits nothing and is counted
(`agent_unpriced_usage_total`), so unknown is never silently free. `CREDITS_PER_USD` has no shipped default (open decision O1).

## Money in

### `billing_customers` — tenant ↔ provider customer

`tenant`, `provider`, `customer_ref`, `created_at`; **`UNIQUE (provider, customer_ref)`** and **`UNIQUE (tenant, provider)`**. Written by the
app when it creates the provider-side customer at checkout, never from a webhook. This is the only way a webhook reaches a tenant.

### `credit_products` — the server-side catalog

`provider`, `product_ref`, `credits NUMERIC(18,6)`, `expires_after_days` (nullable), `active`. A purchase's value is read from here.

*(Built in PR 4: `postgres-init/22-billing.sql`, `app/billing/{store,webhooks,inbox}.py`. Refinements to the sketch below: `billing_customers` has `PRIMARY KEY (provider, customer_ref)` and `UNIQUE (tenant, provider)`; `credit_lots` gains a unique index `(tenant, provider, external_ref)` for purchase and subscription lots, so one payment is credited once even when a provider describes it in two events; the inbox has a trigger that makes `applied` and `ignored` terminal and an event's id, type, payload and receive time immutable, while `quarantined` may be reopened by an operator; `payload` holds only `BillingEvent.stored()`, a closed whitelist, never the raw body.)*

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
   ├──────▶ expired      (age ≥ 30 days, inside Stripe's 35-day window: counted and alerted, never silently dropped)
   └──────▶ failed       (added in PR 5: a permanent refusal, the attempt budget spent, or no customer link left; alerted)
```

*(Built in PR 5: `postgres-init/23-usage-export-outbox.sql`, `app/billing/export.py`, `scripts/billing_export_worker.py`. Refinements to the sketch above: a fourth status, `failed`,
because "bounded attempts" needs somewhere to end that is not `sent` and not `expired`; an event cannot be exported under another tenant, enforced by a **trigger** and not by a composite foreign key like the wallet's (a composite key would need a second
unique constraint on `usage_events`, and **that breaks `ON CONFLICT (event_id)` under concurrency**: two writers of one event can collide on the non-arbiter index and get a `UniqueViolation` instead of a no-op;
found by a real-Postgres test failing 3 runs in 40, and now pinned by a test that `usage_events` has exactly one unique index); a trigger also makes
`sent`/`expired`/`failed` terminal and a row's identity immutable; **age is measured from the event's `occurred_at`**, the timestamp the provider sees, not from when the row was queued; an expiry
pass only touches the providers the worker serves; the age limit is capped at 34 days by the setting itself, since a limit past Stripe's 35-day window would turn a retry into a silent discard.
The foreign key also means an event with an outbox row cannot be deleted, which is the guard behind D7: a retention job must clear finished outbox rows first, on purpose.)*

A worker claims rows with `FOR UPDATE SKIP LOCKED`, so replicas never double-send; the provider-side idempotency key (the event id) is
the second layer, because a crash between "sent" and "marked sent" is the one window the first layer cannot see.

## Metrics (each degrade path counted; alerts for the paths that hide money)

`usage_event_write_failed_total` (**alert**: a lost event is lost revenue), `usage_event_unpriced_total`, `credit_debit_overdraft_total`,
`credit_enforcement_refused_total`, plus two degrade paths on `agent_cost_governance_degraded_total` (**alerts**): `credit_debit` (`CreditDebitFailing`: the event was kept, its charge failed) and `credit_read` (`CreditGateUnenforced`: the gate could not read a wallet), `agent_billing_webhook_total{provider,outcome}` (`applied|duplicate|ignored|quarantined|retry|failed|invalid_signature|invalid_payload|unknown_provider|too_large`; `provider` is a configured adapter or the fixed `unknown`, never the caller's own string),
`quarantined` (**alert** `BillingWebhookQuarantined`: a customer paid and nobody is retrying) and `failed` (**alert** `BillingWebhookFailing`), `agent_usage_export_total{provider,outcome}` (`sent|retry|failed|expired`; **alerts** `UsageExportExpired` and `UsageExportFailed`), `agent_usage_export_oldest_pending_age_seconds` (a per-provider gauge set each worker pass; **alert** `UsageExportStuck` at 7 days),
the degrade path `agent_cost_governance_degraded_total{path="export_enqueue"}` (**alert** `UsageExportEnqueueFailing`: the event was kept, queuing it failed), `agent_credit_reconcile_max_drift_usd` (**built in PR 6**; a gauge: the largest tenant-day difference ABOVE tolerance in the last pass, so no tenant label; the per-tenant detail is in the report; **alert** `CreditReconcileDrift` at `> 0` for 30m: the threshold is the operator's own `CREDIT_RECONCILE_TOLERANCE_USD`/`_PCT`, applied before the gauge is set, so there is one place to tune it), `agent_credit_reconcile_total{outcome}` (`ok|drift|incomplete|failed`; **alert** `CreditReconcileNotCompleting` on `failed|incomplete`: a pass that proves nothing must not read as an all-clear), `agent_credit_granted_total{source}` and `agent_credit_debited_total{kind}` (credits, counted when applied, inside the caller's transaction, so a rollback over-counts: a rate for the dashboard, not the record), and `agent_credit_outstanding{state=available|debt}` (a gauge set by each reconciliation pass; no tenant label). **A sync gauge reaches Prometheus for only five minutes per set** (the SDK exports it once and the collector's exporter forgets a series five minutes after its last update; both verified), so the reconciliation worker re-sets its gauges every minute.
