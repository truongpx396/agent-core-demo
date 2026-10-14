# Roadmap and open limits of the billing package

The one place that lists what is **not** built, in the order it should be taken, with where each item's reasoning lives. It exists because the same facts were scattered across a design note, pattern files, PR bodies and task lists, and a reader should not have to assemble them (constitution Principle VIII: known limitations are disclosed, never omitted). Update it in the PR that closes or adds an item.

*Last updated 2026-10-14, after specs 010, 011 and 012 (PRs #134-#164).*

## Where this is going

**A turnkey billing service for one SaaS** (spec 011 D16, D17): one instance and one database per product, never shared between products. A SaaS developer starts one container, gives it a few settings (their Stripe or Polar keys, the webhook secret, their products and prices) and calls its API; they write no billing code. The service owns the wallet, the ledger, expiry, usage events, reconciliation, sweeps and schema upgrades. The "tenants" inside it are that SaaS's own customers or workspaces.

Whether to build that or adopt something is **not decided** (item 0).

## What exists today

| Capability | Where | Spec / PRs |
|---|---|---|
| Credit wallet: lots, expiry, overdraft, exact decimals, per-tenant serialisation, append-only ledger | `credits.py`, `sql/20-credit-wallet.sql` | 010 |
| Webhook intake: signature, inbox, quarantine, refunds and clawbacks, retention sweep | `webhooks.py`, `inbox.py`, `http.py`, `sql/22-billing.sql` | 010 |
| Stripe and Polar adapters, verified against each provider's own library and a real sandbox, including a paid hosted checkout | `providers/` | 010 (#145-#157) |
| The boundary: the package imports nothing from the agent app, the app reaches it through one port | `port.py`, `app/billing_integration/` | 011 (#158-#162) |
| An independent package with its own tests, SQL, alerts, dashboard, docs, CI job | `packages/billing/` | 011 (#161) |
| Operator CLIs: `credit-billing` (grant, adjust, show), `credit-billing-inbox-sweep`; fast failure, exit statuses, audit log fields | `cli/` | 011 (#162) |
| A migration tool: `credit-billing-migrate` | `migrate.py`, pattern B1 | 012 (#163) |
| An idempotency conflict: a reused key with a different request is refused | `credits.py`, pattern B2 | 012 (#164) |

## Open work, in the order to take it

Spec numbers are assigned by `/speckit-specify` when a feature is created (the next is **013**); the labels below are an order, not reserved numbers. Each item should go through the Spec Kit flow (specify, plan, tasks, implement) and ship as small, reviewable PRs.

### 0. Decide: build it or adopt something (a decision, not code)

Never weighed. Lago and Flexprice are AGPL-3.0 (established, research R11 of spec 011), which matters for a product others run. Autumn and OpenMeter exist as open-source projects whose licences were **not** checked, and Stripe routes LLM token billing through Metronome. This decides how much of items 2 and 3 is worth writing, so it comes first and is cheap: a short comparison against this package's real requirements (prepaid packs, exact decimals, expiry, overdraft, webhook quarantine, one instance per SaaS). *Source: `design-note.md` section 7; spec 011 research R11.*

### 1. The pro-grade wallet contract, still in-process

Three extensions a general service needs. They change the money path and the agent's gate, so each needs the same proof discipline as 012 (tests that fail without the change, a mutation check, existing suites passing unchanged).

- **Holds:** reserve, capture, release with a TTL sweep, so the agent's gate stops computing `wallet.available − reserved` itself (`budget_holds`, `app/agent/budgets.py`). New table and port operations. *Source: design note section 4; spec 011 D16.2, D18; research R4.*
- **A service-side generic usage-event store with an idempotent `record_usage`:** (id, tenant, meter, quantity, time, dimensions), the debit atomic with the event; reconciliation of events against wallet entries and retention sweeps move into the service. The agent keeps its LLM-shaped local meter permanently (its caps need low-latency local reads); its export outbox (script 23) becomes the delivery path to the service's ingest endpoint. Two stores, one flow, no shared table. It will also need a stated policy for **late and backdated events** (a cut-off with a grace window, or a true-up next period; research R12), which nothing decides today. *Source: spec 011 D17; design note section 4; research R2, R8, R12.*
- **Outbound notifications:** low balance, exhausted, refunded, so a SaaS does not poll. *Source: research R10.*

### 2. The service shell

An HTTP API over the port, service-to-service authentication, an entry point and a container, health and readiness, and an HTTP client that implements `WalletPort`. It carries **the consistency change**: today the debit commits in the same Postgres transaction as the usage event; a remote wallet is an at-least-once consume keyed by the event id, the balance lags the meter, and holds (item 1) bound the over-spend window. It also decides when to run `migrate()` (never automatically today). *Source: `design-note.md` sections 3, 4, 6.*

**A trigger is needed, not just a wish.** Without one of these, a service is a network hop on the path of every model call for nothing: a second product sharing one wallet and provider account; isolating the provider credentials and the unauthenticated webhook endpoint from processes that run model output; or billing needing its own deploy cadence, scaling or compliance scope (`design-note.md` section 1).

### 3. Turnkey operation

The product catalog and customer link as configuration (today SQL with no CLI or route); a checkout route (nothing calls `create_checkout` yet); balance and ledger endpoints; a `check`/`track`-style client; a verified setup per provider; a check of cash collected against credits granted; a configurable spend priority. The field's shape for "quick to set up" is a three-call API (attach, check, track) with pricing as configuration (research R9). Rating (`credits_for_cost`, `CREDITS_PER_USD`, `MARKUP`) moves behind the port, and the metric names lose the `agent_` prefix (a dashboard and alert migration). *Source: spec 011 D16.4, D17, D18; design note sections 5, 6.*

### Provider side (independent of 1-3)

- **PayPal:** no adapter. It has no usage ingestion, so it fits only as a prepaid-pack seller.
- **Disputes and partial refunds** are quarantined for a person: what they are worth in credits is a product decision nobody has made. *Source: `webhooks.py` module docstring.*
- **Polar has no dispute events** in the adapter.
- **Usage-based billing to a provider** (the export outbox) is built but off: the maintainer chose prepaid packs only (model A, 2026-10-08).

## Known limits of what is built

| Limit | Why it is accepted | Recorded in |
|---|---|---|
| The migration tool applies **repeatable** idempotent scripts only: no immutable versioned migrations (a change that cannot be written idempotently, or needs a data backfill), no downgrade, and a hand-altered database is not detected (a checksum is about the script, not the live schema) | the shipped scripts are idempotent and edited in place, as the repo has always evolved them; the record table does not preclude versioned migrations | pattern B1 |
| Nothing runs the migration tool automatically | applying schema is an operator or pipeline step; a service shell may choose to | pattern B1 |
| Re-applying a changed script to a busy production table waits up to the lock timeout per DDL statement, then gives up cleanly | it does not schedule itself for a quiet moment | pattern B1 |
| Rows written before `credit_transactions.request` keep the old "any request under a used key is a duplicate" behaviour for ever | their original request is unknowable and the table is append-only, so it is never backfilled | pattern B2 |
| No metric or alert for an idempotency conflict | it surfaces through each caller's existing signal plus a `credit_idempotency_conflict` log event (field names, never values); a counter needs an alert threshold and there is no evidence for one | pattern B2 |
| Idempotency keys are never pruned | a ledger keeps them; Stripe prunes after 24 hours | pattern B2 |
| `compare=` (choosing which fields identify a request) is an escape hatch that can hide a real difference if misused | two call sites, each commented; an unknown name is refused | pattern B2 |
| `reason` and `actor` are compared, so correcting a typo under a reused key needs a new key | it means the audit text just typed was not recorded | pattern B2 |
| A wrong database password is reported as "cannot open a connection" | the pool cannot tell it from an unreachable server within the wait | `cli/_runtime.py` |
| The host's `sql_store` pool keeps psycopg's 30 s connect wait | it is a long-running server, not a command; untouched | spec 011 follow-up (#162) |

## Measurements nobody has made

- Throughput and latency of a remote wallet: no number exists; vendor numbers in the research are claims.
- Whether Postgres row locks per tenant are enough at a service's scale. Autumn reports they were not for a very hot customer and keeps a separate fast path (a cached "spendable balance") beside the authoritative ledger (research R5, their own report); this repo has no load number for the wallet, so whether it would need a fast path is unknown.

## How every follow-up starts

- **Rules that do not bend:** tenant isolation, no model-callable path to a balance, exactly-once effects (constitution I, II, IV). The package imports nothing from the app; new package patterns are numbered **B3, B4, ...** in [PATTERNS.md](PATTERNS.md).
- **A schema change reaches existing databases through `credit-billing-migrate` and needs a deploy order: migrate BEFORE the code that uses it** (the `request` column showed what the other order does: every keyed write fails). The host's `postgres-init/20-` and `22-` copies stay byte-equal to the package's.
- **Verify the way CI runs, not the way a Mac runs:** a subprocess does not inherit pytest's `pythonpath`; a bind mount keeps real file ownership on Linux; test the **installed** command, not the source tree; every job that starts the app installs the package first (`tests/billing/test_ci_installs_the_package.py`). PR #161's first CI run failed both ways.
- **Real provider sandbox** (`make test-provider-sandbox`): only when asked, once, never in a loop (Polar rate-limits); a pytest fixture error prints the fixture's argument values, so redact any log before keeping it.
