# Contract: the billing port (written in PR A's plan, finalised and partly implemented in PR B)

> **Living copy** (spec 011 D13). This is the version to read and edit. The original, `specs/011-billing-module-boundary/contracts/billing-port.md`, is the historical record of the decision it was written for and is not updated. A path in the text is relative to `packages/billing/` unless it names the agent application (`app/...`), which hosts this package in this repository.

The port is **shaped as the service API** (D18) so that a network implementation can satisfy it later. PR B implements the three operations the host uses today (marked ●); the rest are contract only (○) and are built by 012-014. Every operation is tenant-scoped, carries an idempotency key, and states what it does when the wallet cannot be reached. Types are plain: strings, `Decimal`, dataclasses. The one exception is the caller's connection (the in-process form only), because the charge is atomic with the caller's own write.

| | Operation | Idempotency key | Atomic with the caller? | When the wallet is unreachable |
|---|---|---|---|---|
| ● | `record_usage_in(conn, tenant, amount, *, event_id, kind, pricing=None) -> Applied` (today `credits.debit_in`; `event_id` is both the idempotency key and the lineage id, `kind` the call's kind in the reason `model call (<kind>)`) | `event_id` (replay returns the existing transaction) | **yes**, in the caller's transaction and savepoint | the caller's savepoint undoes only the debit; the event is kept (`CreditDebitFailing`) |
| ● | `account_balance(tenant) -> Balance \| None` | none (read) | no | the gate's `CREDIT_CHECK_FAILURE_POLICY` decides (`open` default, counted, alerted) |
| ● | `credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal` | none (pure) | n/a | n/a. Rating stays in the host through 011 (O1) |
| ○ | `check(tenant, amount) -> allowed, balance` | none | no | fail-open by default |
| ○ | `reserve(tenant, amount, *, key, ttl)` / `capture(hold, actual, *, key)` / `release(hold)` | `key` | n/a | a hold expires on its TTL |
| ○ | `grant(tenant, credits, *, key, source, …)` / `adjust(…)` | `key` | n/a | caller retries with the same key |
| ○ | `checkout(tenant, product, *, key, …)` | `key` (provider-side idempotency) | n/a | caller retries |

## Idempotency semantics
A key stands for ONE request (spec 012). The transaction stores a normalised description of the request (`credit_transactions.request`) and a reused key is compared with it: the same request returns the existing transaction (`status="duplicate"`) and changes nothing; a DIFFERENT request raises `IdempotencyConflict`, naming each field that differs, and changes nothing. That is what a service API called by arbitrary clients needs, and what Stripe's does (research R7): a remote implementation maps the exception to a 409-style response. Which fields identify a request is an explicit, validated choice per call (`compare=`; the default is every field and the kind is always compared); a transaction written before the column existed has no stored request and behaves as it always did.

## Errors
`BillingNotConfigured` (no connection supplied), `InvalidSignature` and `InvalidPayload` (webhook), `IdempotencyConflict` (a `ValueError` subclass: a key reused for a different request), and the existing `ValueError`s for a negative or empty debit. A network implementation maps these to status codes; it adds no new meaning.

## Where the pieces live (D19)
- **The contract** is `src/credit_billing/port.py`: `WalletPort`, the in-process implementation and the plain types. A network client would implement the same protocol.
- **The host's one seam** is `app/billing_integration/port.py`: it holds the implementation in use (`wallet`), exposes the operations as functions, and importing it wires the core.
- **What the host may import:** outside `src/credit_billing/`, `app/billing_integration/` and the two core operator scripts that move into the package in PR C, nothing under `app/` or `scripts/` imports `credit_billing`. `app/billing_integration/` itself may use the `credits` read functions, the provider registry and the core table names (a listed exemption that 012/013 removes when wallet-side reconciliation moves into the service).
