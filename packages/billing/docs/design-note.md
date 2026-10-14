# Design note: from a package to a service

> **Living copy** (spec 011 D13). This is the version to read and edit. The original, `specs/011-billing-module-boundary/design-note.md`, is the historical record of the decision it was written for and is not updated. A path in the text is relative to `packages/billing/` unless it names the agent application (`app/...`), which hosts this package in this repository.

**Status**: written in PR B of spec 011, moves into `packages/billing/docs/` in PR C. **Audience**: whoever decides, later, to run billing as its own service for a SaaS. It records what the preparation guarantees, what turning the package into a service would cost, and what has to be built first. Nothing here is built; every claim about the code was read on 2026-10-14, and the claims about how other billing products work come from `research.md` (vendor documentation, not benchmarks).

## 1. When to do it, and when not to

The package is a library the agent app calls in-process. It stays one unless one of these is true:

1. **A second product must share one wallet and one provider account.** (One instance per SaaS is the decided model, spec D16.1; a shared instance would also need a project namespace, a schema change.)
2. **The provider credentials and the public webhook endpoint should not live beside the agent's workers.** The webhook route is unauthenticated until its signature verifies, and it holds a signing secret and a provider API key. Isolating that blast radius from processes that run model output is a real reason on its own.
3. **Billing needs its own deploy cadence, scaling or compliance scope.**

Without one of these, a service is a network hop on the path of every model call, a second thing to keep up, and a new way to be unavailable, in exchange for nothing.

## 2. What the preparation guarantees

- The core imports nothing from the agent app (tests/billing/test_billing_core_boundary.py, 70 tests).
- The agent app reaches the wallet through exactly two operations behind a protocol (`src/credit_billing/port.py::WalletPort`), selected in one place (`app/billing_integration/port.py::wallet`). A different implementation is a one-line change there, and a stub of it already drives the charge, the gate and `GET /usage` in a test.
- No host file names a core table in SQL, and none imports the core except the listed exemptions (§5).
- The write is idempotent on its key `(tenant, event_id)` and the read is a pure read, which is what makes both safe to put behind a network.

## 3. The port on the wire

| Port operation | A network form | Idempotency | Failure when unreachable |
|---|---|---|---|
| `record_usage_in(conn, tenant, amount, event_id, kind, pricing)` | `POST /v1/accounts/{tenant}/debits` with `event_id`, `amount`, `kind`, the pricing snapshot | the `event_id` (a replay returns the first result) | the caller's savepoint undoes only the debit; the event is kept; counted `credit_debit` (alert `CreditDebitFailing`) |
| `account_balance(tenant)` | `GET /v1/accounts/{tenant}/balance` returning `available`, `ledger`, `debt`, or 404 for "no wallet" | none (read) | `CREDIT_CHECK_FAILURE_POLICY`: `open` serves the turn and counts it, `closed` refuses it |
| `credits_for_cost(...)` | stays a local pure function through spec 011; moves behind the port when rating does (O1) | n/a | n/a |

The webhook route (`src/credit_billing/http.py`) is already a network surface and does not change: the provider calls it, signature first.

**What does not survive the move as written:** `record_usage_in` takes the caller's database connection. A remote wallet cannot. That is the one non-plain parameter in the port, and it is the next section.

## 4. The consistency change a remote wallet implies

Today the debit is in the same Postgres transaction as the usage event: the event insert and the wallet debit commit or roll back together, inside a savepoint, so a replay cannot double-debit and a failed debit never loses the event (`usage_events._charge`). A service cannot offer that. The replacement is **an at-least-once consume keyed by the event id**:

- The agent keeps writing its own usage event (the dollar caps need it, and `USAGE_EVENTS_ENABLED=false` is refused at startup). It then *delivers* the event to the service, with the event id as the idempotency key. The delivery machinery already exists in this repo in another form, the export outbox (`app/billing_integration/export.py`: bounded retries, a call deadline, an age limit that expires loudly, an alert on every way it can stall). The service's ingest endpoint would be a new destination for the same pattern.
- **The balance lags the meter** by the delivery delay. That is acceptable because the wallet already tolerates an inexact balance: a debit never fails for a short balance, the shortfall is booked as overdraft and repaid by the next grant (`credits.debit_in`).
- **But lag widens the window for over-spend.** A turn can start while earlier turns' debits are still in flight. Today the gate bounds this with `wallet.available − reserved`, where `reserved` is the agent's own in-flight reservation (`budget_holds`, `app/agent/budgets.py::_credit_check`). A service should own that as a primitive (reserve, capture, release with a TTL, as Chargebee and Convex Credits do; research R4), or each client re-implements the race protection. This is extension 2 below.
- **The gate's failure policy already exists.** `CREDIT_CHECK_FAILURE_POLICY` (`open` by default) decides what a turn does when the wallet cannot be read; it was designed for a wallet that can be down, which is the case a service adds. Autumn's SDKs make the same default choice (research R6).
- **Reconciliation splits.** The comparison of usage events with the gateway's spend log stays in the agent (the service cannot know the gateway). The comparison of events with wallet entries moves into the service, which then needs its own copy of the events (spec D15).

## 5. What is deliberately still coupled

| Coupling | Why it stays | Removed by |
|---|---|---|
| `tenant` is the opaque key everywhere; `usage_event_id` and the `agent_` metric prefix name the agent's concepts | renaming changes the schema and every dashboard (D3, D4) | a service feature, with a migration |
| Rating (`credits_for_cost`, `Pricing`, `CREDITS_PER_USD`, `MARKUP`) is the host's | its signature is on the debit path (O1) | 013/014 |
| `app/billing_integration/export.py` and `reconcile.py` read core tables (`billing_customers`, `credit_*`) and the agent's `usage_events` | they are the two consumers of the agent's meter; D5 | 012/013 moves wallet-side reconciliation into the service |
| `app/core/config.py` imports `credit_billing.settings` (the host's `Settings` inherits `BillingSettings`) | one definition of every billing variable | PR C, when the package ships its own settings |
| `scripts/credits.py` and `scripts/billing_inbox_sweep.py` import the core directly and run on the agent's `scheduled_job` bootstrap | they are the core's own operator CLIs | PR C moves them and gives the package its own bootstrap (D11) |
| the webhook rate limit is a host middleware on `/billing/webhooks/` (`app/api/rate_limit.py`) reading a core setting | the limiter is a host concern | PR C documents it; a service brings its own |
| the OTel meter scope string is still `app.core.metrics` | the collector turns scope into a label and does not merge series (research P6) | PR C, with the label sets pre-created at 0 |

## 6. What has to be built before any of it

1. **A migration tool.** *(Done: spec 012 PR A, `credit-billing-migrate` and `credit_billing.migrate`.)* Schema scripts used to run only on a fresh volume (`postgres-init/NN-*.sql`) or by hand once. A service that owns its schema, deploys on its own cadence and runs against a database that already has data needs real migrations, and now has the first kind: the shipped idempotent scripts are applied in order, recorded with a checksum, serialised across runners and bounded in every wait (pattern B1). Immutable one-way versioned migrations, for a change that cannot be written idempotently or needs a backfill, are the next step and are not built.
2. **A service shell** (spec 013): an HTTP API over the port, service-to-service authentication, an entry point and a container, health and readiness, and an HTTP client that implements `WalletPort`.
3. **The four extensions a general service needs** (spec D16, research R4, R7, R10): holds; ~~a conflict error when an idempotency key is reused with different parameters~~ *(done: spec 012 PR B, `IdempotencyConflict`, pattern B2)*; the service-side event store with an idempotent `record_usage`; outbound notifications (low balance, exhausted, refunded) so a SaaS does not poll. Three of the four remain open.
4. **Turnkey operation** (spec 014): the catalog and customer link as configuration, checkout, balance and ledger endpoints, a `check`/`track`-style client, a verified setup per provider.

## 7. What this note does not establish

- Throughput or latency of a remote wallet: no measurement exists, and the vendor numbers in `research.md` are claims.
- Whether Postgres row locks per tenant are enough at a service's scale. Autumn reports they were not for a very hot customer (research R5, vendor's own report); this repo has no load number for the wallet either.
- Build versus adopt: Lago and Flexprice are AGPL, Autumn and OpenMeter are open source, and Stripe routes LLM token billing through Metronome (research R11). It was not weighed here.
