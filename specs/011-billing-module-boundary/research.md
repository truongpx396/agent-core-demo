# Research: how a professional team builds a self-contained billing service for AI SaaS products

**Date**: 2026-10-13. **Question**: what do the well-regarded billing products and write-ups converge on, how does this repo compare, and what should feature 011 (the boundary) and its follow-ons take from it?

**Read this first.** Almost every source is vendor-authored (Autumn, Flexprice, Lago, Chargebee, Stripe/Metronome, OpenMeter) or a practitioner blog. Throughput and latency figures are the vendors' own claims and were not benchmarked. Where a claim comes from a search summary and not from a page I read, it is marked *(summary)*. Nothing here was compared by running the products.

## Findings

**R1. Metering and pricing are separated from payment processing.** The app-facing billing layer owns usage, pricing and entitlement; the payment provider only moves money. Autumn: "Stripe still holds the subscription and processes payments" ([welcome](https://docs.useautumn.com/welcome)). Stripe's token-billing page: Metronome defines rates and records usage, "evaluates usage in real time and sends invoices to Stripe for payment" ([docs](https://docs.stripe.com/billing/token-billing)). *This repo already does this* (the app owns the wallet, the provider owns money, spec 010 D2).

**R2. Usage is ingested idempotently by a producer-generated id, and kept append-only.** Lago deduplicates on an event `transaction_id` ([reconciliation post](https://getlago.com/blog/usage-based-revenue-reconciliation)); Polar ingests events by name, customer and metadata ([docs](https://polar.sh/docs/features/usage-based-billing/event-ingestion)); a practitioner guide says to store both event time and ingest time and to rate on event time ([dev.to](https://dev.to/nkassetty/usage-based-billing-a-practical-guide-for-engineers-1ncc)). *This repo matches:* `usage_events.event_id` is the primary key with `ON CONFLICT DO NOTHING`, and it stores `occurred_at` and `recorded_at`.

**R3. Credits are a ledger of typed entries with buckets, expiry and a spend order.** Metronome: a credit's balance is the sum of its ledger entries ([docs](https://docs.metronome.com/guides/customers-billing/optimize-customer-experience/get-remaining-balance)). Lago wallets have a priority from 1 to 50 and the soonest-expiring wallet goes first on a tie *(summary of [Lago docs](https://getlago.com/docs/guide/wallet-and-prepaid-credits/overview) and a Lago blog)*. Autumn describes each bucket as its own row, spent in waterfall order ([blog](https://useautumn.com/blog/ai-billing-infrastructure.md)). *This repo matches* on the ledger, expiry, and earliest-expiring-first; it has no configurable spend priority.

**R4. A hold (authorize, then capture or release, with a TTL) is the primitive that stops over-spend.** Chargebee: authorize moves credits from usable balance to a hold, an unreleased hold returns on expiry, capture consumes all or part and returns the rest ([authorize](https://api-static-site.au.chargebee.com/docs/api/ledger_operations/authorize)). Convex Credits offers reserve, complete and release with stable operation keys ([page](https://www.convex.dev/components/clipin/convex-credits)). Autumn gives the reason: with asynchronous deduction a user with 600 credits can have five parallel 500-credit sub-agent runs approved, so it estimates, deducts up front, then adjusts or refunds ([blog](https://useautumn.com/blog/ai-billing-infrastructure.md)). *This repo differs:* a debit never fails (a shortfall is booked as overdraft), and the in-flight protection is the host's `budget_holds` plus `wallet.available − reserved` in `app/agent/budgets.py`. The wallet has no hold primitive.

**R5. The real-time check is a separate fast path from the authoritative ledger.** Autumn caches one derived "spendable balance" per customer in Redis and reports sub-20ms reads, and says Postgres transactions could not sustain "even a couple of hundred deductions per second", so it moved counter writes to Redis Lua scripts (vendor's own report, accepting a drift risk that needs reconciliation) ([blog](https://useautumn.com/blog/ai-billing-infrastructure.md)). OpenMeter keeps usage in ClickHouse via Kafka, a balance worker publishing balance snapshots, and Postgres as the source of truth for entitlements ([architecture](https://openmeter.io/docs/open-source/architecture)). *This repo:* Postgres only, a per-tenant lock and row locks. That is right at this scale; the scale-out path is a design-note item, not work.

**R6. The default failure mode of the check is fail-open.** Autumn's SDKs are "fail-open and fail-fast", and usage is reconciled afterwards ([welcome](https://docs.useautumn.com/welcome)). *This repo matches:* `CREDIT_CHECK_FAILURE_POLICY=open` by default, counted and alerted.

**R7. An API's idempotency has defined semantics.** Stripe: keys up to 255 characters, the first response (including a 500) is saved and replayed, the parameters are compared and a mismatch is an error, keys may be pruned after 24 hours ([docs](https://docs.stripe.com/api/idempotent_requests)). *This repo differs, verified in `app/billing/credits.py::_begin_transaction`:* the key is kept permanently (stricter than Stripe), but a replay with the same key and a different amount or kind is treated as a replay and returns the existing transaction without comparing anything. That is harmless for a debit keyed by a deterministic event id, and wrong for a service API that any client can call.

**R8. Reconciliation is a bridge across stages, each difference with a reason, an owner and a correction path.** Lago's five quantities: activity, billable units, rated charges, invoiced amount, collected cash ([post](https://getlago.com/blog/usage-based-revenue-reconciliation)). *This repo:* compares events with the gateway's spend log and with wallet entries. As far as I read it, nothing compares the cash a provider collected with the credits granted (a missed webhook is caught only by the quarantine path). To confirm in the plan.

**R9. "Quick to set up" is a three-call API and pricing as configuration.** Autumn's core is `attach`, `check` and `track`; `check` takes a customer id, a feature id and a required balance and returns `allowed`; `track` records the value after the action ([gating](https://docs.useautumn.com/documentation/getting-started/gating)). Pricing changes are "a configuration change rather than a code change", state is replicated into the app by the `customer.products.updated` webhook, and the docs suggest caching customer data on latency-sensitive paths ([welcome](https://docs.useautumn.com/welcome)). Autumn's "under an hour" setup is the vendor's claim. I found no documentation of its config-file format.

**R10. The service notifies the product.** Autumn emits `customer.products.updated`; OpenMeter has a notification service delivering through Svix ([architecture](https://openmeter.io/docs/open-source/architecture)). *This repo has no outbound events*; a SaaS would have to poll the balance.

**R11. Build or adopt is a real first question.** Open-source options exist: Lago (AGPLv3), Flexprice (AGPL-3.0), OpenMeter, Autumn (licences other than Lago's and Flexprice's not established here). A hosted route exists too: Stripe now routes LLM token billing through Metronome ([docs](https://docs.stripe.com/billing/token-billing)). Building is defensible here (the app-owned wallet exists and is tested, the repo is a teaching reference, and AGPL affects embedding), but it was not weighed against adopting.

**R12. Late and backdated events need a stated policy** (a cut-off with a grace window, or a true-up in the next period; closed invoices get corrections, not edits) ([dev.to](https://dev.to/nkassetty/usage-based-billing-a-practical-guide-for-engineers-1ncc)). A prepaid wallet has no invoice period, so a late event simply debits when it arrives; the open question is only which timestamp a statement is cut on.

## What this changes

| Source of the change | Where it lands |
|---|---|
| R1, R2, R3, R6 match the repo | No change. They are why the preparation is a boundary and not a redesign. |
| R4 (holds), R7 (idempotency semantics), R10 (outbound events), R8 (cash vs credits), R9 (config and a three-call client) | Not in 011. Each adds behaviour, and several touch the money path. They become the follow-on features (spec D17), and the 011 port contract is written so each is an additive operation. |
| R5 (fast path vs ledger), R11 (build vs adopt), R12 (late events) | Written in the 011 design note as decisions to take at the trigger, not built. |

## Not established

- Any benchmark: every throughput and latency figure above is a vendor claim.
- Autumn's config-as-code format, Kill Bill's current design, Metronome's internals, and whether Polar is a merchant of record (a search returned nothing on it).
- Any post-mortem of a billing-service failure. The lessons here are design guidance, not incident reports.

---

# Phase 0 appendix: technical findings from the code (for plan.md)

Read on 2026-10-13. Each finding is something the plan's design depends on. P6 was verified by running a real collector; the rest are reads of the code or greps, not run experiments.

**P1. The core's host imports are 7 files, and two of them are lazy.** `credits`, `inbox`, `webhooks`, `export`, `reconcile` import `app.core.config`, `app.core.metrics`, `app.agent.sql_store` or `app.agent.gateway` at the top; `providers/stripe.py` and `providers/polar.py` import `app.core.config` inside their constructors ("read at construction, not import: tests re-point the settings"). A scan of top-level imports misses the last two, so the boundary test walks every import node.

**P2. The settings split is clean.** Ten fields and four validators read only each other (`billing_providers`, `billing_webhook_secrets`, `stripe_api_key`, `polar_*`, `billing_webhook_*`, `billing_refund_hold_hours`, `billing_inbox_retention_days`). Everything else billing-shaped is host policy (`credits_per_usd`, `markup`, `credits_enforcement`, `credit_check_failure_policy`, the export and reconcile fields). No validator spans the two sets. `Settings(BillingSettings)` inheritance therefore keeps every field, validator and env name intact with one definition.
*Alternatives rejected:* duplicating the fields in both classes (drift); composing instead of inheriting (changes every `settings.<field>` reader and the settings tests).

**P3. Tests re-point settings by patching the consumer's own constant**, e.g. `monkeypatch.setattr(webhooks, "BILLING_REFUND_HOLD_HOURS", ...)`, `billing_router.BILLING_PROVIDERS`, `usage_events.CREDITS_PER_USD`. Core modules that keep the same constant names keep those tests unedited. Only three patches use a string path into `app.core.config` (`STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`, in the adapter tests) and must be re-pointed.

**P4. `credits.get_connection` is the only connection name patched in tests** (the autouse guard in `tests/conftest.py` and four integration tests). If each core module keeps a module-level `get_connection` that delegates to the injected provider, none of those sites change. `webhooks` and `inbox` are not guarded today, so the guard's scope is not widened in PR A (that would be a behaviour change to the suite).

**P5. Two tests read `app/core/metrics.py` as text** to find defined metrics (`tests/core/test_alert_rules.py`, `tests/core/test_credit_dashboard.py`), and `metric_value` finds a counter by its `.name`. Moving four counters therefore needs those two scans to read both files and a wrapper that keeps `.name`.

**P6. The collector turns the meter scope into a label (verified).** A throwaway `otel/opentelemetry-collector-contrib` with the repo's exporter config (a bare `prometheus` exporter) received counters from two scopes: every series carried `otel_scope_name`, `otel_scope_schema_url` and `otel_scope_version`, and the same metric name emitted under `app.core.metrics` and under `billing` came out as **two separate series** (no merge, no error). Consequences: renaming the scope forks each series; the dashboard queries use `sum by (...)`, which survives; `BillingWebhookQuarantined` and `BillingWebhookFailing` are bare `increase(...[10m]) > 0` and cannot see the first sample of a series that did not exist before, so a scope change would hide the first event per label set after the deploy. PR A keeps the scope string. (The same property already holds for a label set that has never fired; `reconcile` pre-creates its series with `inc(0)`, the webhook counter does not. That is an existing gap, not one this feature opens; it is recorded here and not fixed.)

**P7. Import-time wiring is safe for a function reference and unsafe for the telemetry provider.** `configure_telemetry` is call-once and installs a network exporter, hence "never at import". Wiring `billing.db.configure(connection=sql_store.get_connection)` stores a callable and opens nothing, so it can run at import, which is what makes it impossible to forget in an entry point. A guard test (every host module that imports `app.billing` also imports `app.billing_integration`) makes forgetting a build failure.

**P8. The host uses 8 symbols of billing**: `credits.debit_in`, `credits.Pricing`, `credits.credits_for_cost`, `credits.account_balance` (and the `Balance` it returns), `providers.usage_export_providers`, `providers.build_configured`, `providers.base.{BillingProvider, InvalidPayload, InvalidSignature}`, `webhooks.process_event`. That is PR B's port, plus the router and the registry.

**P9. Three consumers read `postgres-init/`** (dev and prod compose bind mounts, the deploy rsync which syncs only `postgres-init/` and `observability/`, and `tests/containers.py` which seeds from every `*.sql` in it). A symlink cannot cross a bind mount. Hence O3's decision: canonical SQL in the package, committed copies under `postgres-init/`, byte-equality test.

## Decisions (O1-O4) in the format the template asks for

| | Decision | Rationale | Alternatives considered |
|---|---|---|---|
| O1 | Rating (`credits_for_cost`, `Pricing`) stays in the wallet API through 011 | Used by `budgets` and `usage_events`; its signature is on the debit path and in its tests; moving it is a behaviour change | Move behind the port now (changes the money path in a "no behaviour change" feature) |
| O2 | Import name `credit_billing`; webhook router moves into the core in PR B and into the package in PR C; FastAPI is an optional extra | `billing` collides with other projects' modules; the router needs only FastAPI, settings and metrics, all in the core after PR A | `billing`; leaving the router in the host (the package would have no inbound path) |
| O3 | Package-canonical SQL, committed copies in `postgres-init/`, byte-equality test | P9: three consumers, none changed; schema identical by construction | Single-file bind mounts (changes all three consumers); generating the copies at build time (hides them from review) |
| O4 | Split alerts and dashboard by who emits the metric, delivered like the SQL | Same constraint (Prometheus and Grafana read fixed paths) | One file left in the host (the package would not own its alerts, contrary to FR-013) |
