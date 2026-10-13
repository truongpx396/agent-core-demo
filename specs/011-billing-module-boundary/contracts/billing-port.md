# Contract: the billing port (written in PR A's plan, finalised and partly implemented in PR B)

The port is **shaped as the service API** (D18) so that a network implementation can satisfy it later. PR B implements the three operations the host uses today (marked ●); the rest are contract only (○) and are built by 012-014. Every operation is tenant-scoped, carries an idempotency key, and states what it does when the wallet cannot be reached. Types are plain: strings, `Decimal`, dataclasses. The one exception is the caller's connection (the in-process form only), because the charge is atomic with the caller's own write.

| | Operation | Idempotency key | Atomic with the caller? | When the wallet is unreachable |
|---|---|---|---|---|
| ● | `record_usage_in(conn, tenant, credits, *, event_id, kind, pricing)` (today `credits.debit_in`) | `event_id` (replay returns the existing transaction) | **yes**, in the caller's transaction and savepoint | the caller's savepoint undoes only the debit; the event is kept (`CreditDebitFailing`) |
| ● | `account_balance(tenant) -> Balance \| None` | none (read) | no | the gate's `CREDIT_CHECK_FAILURE_POLICY` decides (`open` default, counted, alerted) |
| ● | `credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal` | none (pure) | n/a | n/a. Rating stays in the host through 011 (O1) |
| ○ | `check(tenant, amount) -> allowed, balance` | none | no | fail-open by default |
| ○ | `reserve(tenant, amount, *, key, ttl)` / `capture(hold, actual, *, key)` / `release(hold)` | `key` | n/a | a hold expires on its TTL |
| ○ | `grant(tenant, credits, *, key, source, …)` / `adjust(…)` | `key` | n/a | caller retries with the same key |
| ○ | `checkout(tenant, product, *, key, …)` | `key` (provider-side idempotency) | n/a | caller retries |

## Idempotency semantics (today, and the gap)
Today a replayed key returns the existing transaction **without comparing parameters** (`ON CONFLICT (tenant, idempotency_key) DO NOTHING`, then `SELECT id`). For a debit keyed by a deterministic event id that is safe. A service API called by arbitrary clients must instead refuse a reused key with different parameters, as Stripe's does (research R7). That is a behaviour change and belongs to 012.

## Errors
`BillingNotConfigured` (no connection supplied), `InvalidSignature` and `InvalidPayload` (webhook), and the existing `ValueError`s for a negative or empty debit. A network implementation maps these to status codes; it adds no new meaning.

## What the host may import
Outside `app/billing/` and `app/billing_integration/`: `app.billing.port` and `app.billing.http` only. `app/billing_integration/` may also use the `credits` read functions and the provider registry (a listed exemption removed by 012/013).
