# Running the billing package: operator runbook

How to turn credit billing on, change a wallet, reconcile it, and sell a credit pack with Stripe or Polar. It moved here from the agent app's `infra/README.md`, because it is
about the package and not about the droplets. Everything here is optional: with `CREDITS_PER_USD` unset the whole feature is off and nothing below applies.

Commands are written as this repository's `make` targets (`make credits`, `make credit-reconcile`, `make billing-migrate`, ...); each runs a command the package installs
(`credit-billing`, `credit-billing-reconcile`, `credit-billing-migrate`, ...), listed in the package [README](../README.md). The things that belong to the agent app and not to
billing (its spend caps, its comparison with the LLM gateway, running the workers on the Digital Ocean droplets) stay in the app's [`infra/README.md`](../../../infra/README.md).

## Three records of the same spending

Since spec 013 billing keeps its own record, and an operator's job is to know they agree:

| Record | Written by | Read for |
|---|---|---|
| `usage_events`, one row per model call | the app, per call (its own meter) | **the dollar caps and `GET /usage`** |
| `billing_usage_events`, one row per usage handed to billing | the billing package, in the same transaction as the agent's row | what a tenant is charged and what is exported |
| the gateway's spend log (LiteLLM, by `end_user`) | the gateway, from the request itself | the independent second meter |

The agent checks the first against the third (`make usage-reconcile`); the package checks the second against the wallet (`make credit-reconcile`, below).

## Turning it on, in the order that cannot hurt

1. Apply the migrations on an existing volume (init scripts run only on a fresh one): `postgres-init/19` by hand, and the billing package's `20`, `22`, `24`, `25` with
   `make billing-migrate` (records what it applied; `make billing-migrate-status ARGS=--check` is the deploy gate). Nothing breaks until the next step: no tenant has a wallet.
2. Set `CREDITS_PER_USD` (no default: the price is your decision) and leave `CREDITS_ENFORCEMENT=false`. This is **shadow mode**:
   tenants with a wallet are debited and nobody is refused, so you can watch balances move first (dashboard "Credit Billing").
3. Give a tenant a wallet and credits: `make credits ARGS="grant --tenant acme --amount 500 --by <you> --reason '<why>'"`.
4. Start the reconciliation workers (`make credit-reconcile-worker`, and the agent's `make usage-reconcile-worker`; the droplet commands are in the app's `infra/README.md`), watch a few days of `make credit-reconcile` and `make usage-reconcile` come back clean, **then** set `CREDITS_ENFORCEMENT=true`.

## Changing a wallet by hand

```
make credits ARGS="show --tenant acme"                                   # balance, lots, newest ledger entries (read-only)
make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up, ticket 4412'"
make credits ARGS="grant --tenant acme --amount 100 --source promo --expires-in-days 30 --by alice --reason 'launch'"
make credits ARGS="adjust --tenant acme --amount=-12.5 --by alice --reason 'call billed twice'"
```

`--by` and `--reason` are required and stored on the transaction; promo and subscription grants must expire; a negative adjustment can
leave a wallet in debt (a correction is never blocked by the mistake it fixes). **A retry is safe only with the same `--key`**: every
change prints its idempotency key, and re-running the command without it is a second change on purpose.

## Reconciling the wallet

```
make credit-reconcile                       # was every recorded usage worth credits actually charged? 0 yes, 1 drift, 2 could not finish, 3 database unreachable
make credit-reconcile-worker                # a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, feeding the gauges and alerts
```

It needs only the database (`APPDATA_DATABASE_URL`). A finding names the tenant, the UTC day and the amounts:

| Finding | Meaning | First step |
|---|---|---|
| `uncharged` | a usage worth credits with no debit: the wallet failed after billing kept the record (`CreditDebitFailing`) | find the cause first; then repair (below) |

**Repairing an uncharged event, for now by hand.** The debit key IS the event id, so booking an adjustment under that key clears the finding
and can never double-charge: `make credits ARGS="adjust --tenant <t> --amount=-<the event's credits> --key <event_id> --by <you> --reason 'uncharged event <event_id>'"`
(the credits are in `billing_usage_events.credits`; the report lists the count and total per tenant-day). It books as kind `adjust`, by you, not as a `debit`. A repair job is not built (disclosed).

No worker is a compose service yet (disclosed). On the droplets they run from the app image; the commands are in the app's [`infra/README.md`](../../../infra/README.md).

## Trimming the billing usage events

Nothing schedules the sweep (run it from cron only once your retention policy is decided). It refuses a window under **35 days**, because a monthly cap and the reconciliations read back that far.

- **`make billing-usage-sweep`** deletes the billing package's usage events older than `BILLING_USAGE_EVENT_RETENTION_DAYS` (400 by default) and prints:
  - **`Cleared N finished export row(s)`**: events already `sent` to a provider go together with their finished export row.
  - **`KEPT N old event(s) whose export never finished`**: events whose export is `pending`, `failed` or `expired` are never deleted, however old, because they are usage that was meant to reach a provider and did not (`UsageExportFailed`, `UsageExportExpired`). Find them with `SELECT provider, status, count(*) FROM billing_export_outbox WHERE status <> 'sent' GROUP BY 1, 2`. Fix the cause and let the worker send them, or, when you have decided that usage will never be sent, delete its outbox row by hand (`DELETE FROM billing_export_outbox WHERE provider = '<p>' AND tenant = '<t>' AND event_id = '<id>'`), after which the next sweep removes the event. There is no "resolved" state yet, so this is a deliberate act.
- **The wallet is never touched by either.** A debit keeps its `usage_event_id`, and the balance does not move. What a deleted event takes with it is the ability to recompute that debit's credits from the event row, so keep events as long as you may need to explain a charge.

## Selling a credit pack with Stripe (sandbox first)

(specs/010 T029a. The model is prepaid packs: Stripe takes the money and signs a webhook; the wallet above is the real-time gate. Stripe is never told what was
consumed. The Polar adapter is not built.)

1. **In the Stripe sandbox**, create a Product and a one-time Price for the pack. The **Price id (`price_...`) is the catalog's `product_ref`.** With **Managed
   Payments** on (the default for a new account) Stripe refuses a Checkout line item whose Product has no `tax_code` (HTTP 400, "the product tax code is missing");
   set an eligible one on the Product (e.g. `txcd_10103001`, SaaS for business use; the list is `GET /v1/tax_codes`). Found by the real sandbox run, not by any document.
2. **The signing secret.** A Dashboard webhook endpoint pointed at `https://<your host>/billing/webhooks/stripe` shows a `whsec_...`; locally, `stripe listen --events
   checkout.session.completed,checkout.session.async_payment_succeeded,charge.refunded,charge.dispute.created,charge.dispute.closed --forward-to localhost:8000/billing/webhooks/stripe`
   prints one. Subscribe to exactly those events: others are acknowledged and ignored.
3. **`.env`** (API, agent workers and export worker alike): `BILLING_PROVIDERS=stripe`, `BILLING_WEBHOOK_SECRETS={"stripe":"whsec_..."}`, and `STRIPE_API_KEY` (a `sk_test_`/`rk_test_`
   key) if anything will start a checkout. Without the key the adapter still verifies deliveries and refuses to create a checkout.
4. **The catalog and the customer link are SQL today (disclosed: there is no CLI for them).** A pack and the Stripe customer that belongs to a tenant:

```
psql -U langfuse -d appdata -c "INSERT INTO credit_products (provider, product_ref, credits) VALUES ('stripe', 'price_...', 5000)"
psql -U langfuse -d appdata -c "INSERT INTO billing_customers (tenant, provider, customer_ref) VALUES ('acme', 'stripe', 'cus_...')"
```

   A purchase for a customer or a price that is not in these tables is **quarantined, not guessed at** (`unlinked_customer`, `unknown_product`, and `no_customer` for a guest checkout; alert `BillingWebhookQuarantined`).
5. **Nothing in the API starts a checkout yet.** `StripeProvider.create_checkout` works (the sandbox test calls it) but no route calls it, so today a pack is bought only from
   code; a session made any other way (a Payment Link, the Dashboard) carries no `metadata.product_ref` and is quarantined as `unknown_product`.
6. **Prove the wiring** with the real sandbox: `make test-provider-sandbox` creates labelled test objects, starts `stripe listen`, and checks real signed deliveries (needs the
   `stripe` CLI logged in to the same sandbox). It **skips while you have your own `stripe listen` (or `polar listen`) running**: every trigger is delivered to every listening session of the
   account, so your local app would receive the tier's events too. Stop yours, or set `PROVIDER_SANDBOX_SHARE_LISTENER=1` to accept that.



## Selling a credit pack with Polar (sandbox first)

(specs/010 T029b. Same model as Stripe: Polar takes the money and signs a webhook; the wallet is the real-time gate; Polar is never told what was consumed.)

1. **In the Polar sandbox** (sandbox.polar.sh, a separate account and organization from production), create a **one-time** Product with a fixed price. The **Product id (a UUID) is the catalog's
   `product_ref`**. Create a Polar Customer for the tenant; its id is the `customer_ref`. The adapter lists and creates checkouts, so the Organization Access Token needs at least `checkouts:read` and `checkouts:write` (the scopes Polar's SDK lists for those calls; the sandbox run used a token with wider scopes, so the minimum is not verified).
2. **The signing secret.** A webhook endpoint pointed at `https://<your host>/billing/webhooks/polar` shows one; locally, `polar listen localhost:8000/billing/webhooks/polar` forwards the
   organization's events signed with `polar listen --print-secret` (a bare 32-character string, not `whsec_`-prefixed, in the sandbox). Either form works: the adapter tries both keys a secret can mean (Polar's
   docs: a secret made before 2026-09-08 is used as its own UTF-8 bytes, a later one is Standard Webhooks with the base64 remainder decoded). Subscribe to `order.paid` and `order.refunded`; every other event is acknowledged and ignored.
3. **`.env`** (API and workers alike): `BILLING_PROVIDERS=polar`, `BILLING_WEBHOOK_SECRETS={"polar":"..."}`, `POLAR_ACCESS_TOKEN`, and `POLAR_ENVIRONMENT=sandbox` (the default; `production` is a different API and a
   different token). Without a token the adapter still verifies deliveries and refuses to create a checkout.
4. **The catalog and the customer link are SQL today (disclosed: there is no CLI for them):**

```
psql -U langfuse -d appdata -c "INSERT INTO credit_products (provider, product_ref, credits) VALUES ('polar', '<polar product id>', 5000)"
psql -U langfuse -d appdata -c "INSERT INTO billing_customers (tenant, provider, customer_ref) VALUES ('acme', 'polar', '<polar customer id>')"
```

   Unlike Stripe, an Order carries the product and the customer directly, so nothing travels through metadata. A purchase by a Polar customer that is not linked (a buyer who checks out without
   being pre-linked may arrive as a customer this app has never heard of) or of a product that is not in the catalog is **quarantined, not guessed at** (`unlinked_customer`, `unknown_product`).
5. **Nothing in the API starts a checkout yet.** `PolarProvider.create_checkout` works, and because Polar has no idempotency for it (a sent `Idempotency-Key` is ignored, verified) it first searches the
   customer's newest 100 sessions for the product for the same attempt and only then creates one. Two concurrent calls with the same key can each create a session; only an unpaid session results.
6. **What Polar does not tell you.** There is no dispute or chargeback event in Polar's webhooks, so `DISPUTE_OPENED`/`DISPUTE_CLOSED` are never produced and a chargeback is invisible to the wallet until
   it shows up as a refund. A renewal's `order.paid` / `order.refunded` (any `billing_reason` but `purchase`) is ignored.
7. **Prove the wiring** with `make test-provider-sandbox` (see Stripe, step 6, for the listener caveat). The checkout half runs against the real API; the delivery half needs the `polar` CLI logged in to the same organization.


## Dashboard and alerts

"Credit Billing" in Grafana (`observability/grafana/dashboards/credit-billing.json` in the app, `observability/credit-billing-core.json` in the package): credits outstanding and owed, grant and debit rates, export
lag, webhook outcomes, the reconciliation's drift and outcomes. There is no Postgres datasource, so who holds what is `make credits ... show`.

| Alert | Means | Do |
|---|---|---|
| `CreditReconcileDrift` | the last wallet pass found usage worth credits that was never debited | `make credit-reconcile` |
| `CreditReconcileNotCompleting` | a wallet pass failed | log `credit_reconcile_failed`; check the database |
| `BillingEventWriteFailing` | a usage was handed to billing and was not recorded: it will never be charged or exported | `billing-migrate-status`, the database; then the caller's own reconciliation to size the gap |
| `CreditDebitFailing` | a usage was recorded and its wallet debit failed | find why; the next `make credit-reconcile` names the events |
| `UsageExportStuck` / `Expired` / `Failed` / `EnqueueFailing` | usage is not reaching a provider that bills on it | `billing_export_outbox` (see the alert text) |
| `BillingWebhookQuarantined` / `Failing` | a customer paid and was granted nothing, or applying a payment keeps failing | `billing_webhook_events` |

## What this does not cover (disclosed)

- **The gauges exist only while a worker runs** (`credit-reconcile-worker`, the export worker). A synchronous OpenTelemetry gauge is exported once per set and the collector forgets
  a series five minutes after its last update, so the worker re-sets them every minute. A stopped worker therefore **resolves** `CreditReconcileDrift` and `UsageExportStuck` rather than
  leaving them firing, and Prometheus cannot tell a worker that never ran from one that died. Cron plus the `make` command still gives the report and the exit code; it gives the alert
  nothing durable. (This holds for opentelemetry-sdk 1.44; see the known-limits table in [ROADMAP.md](ROADMAP.md) for 1.45.)
- **A provider's own balance** is not reconciled: the port has `read_balance` (`BALANCE_READ`) and no adapter declares it yet, so there is nothing to
  compare. It lands with the first adapter that does, through the same contract test.
- **Events that were never queued for export** are alerted by `UsageExportEnqueueFailing` when the failure happens, but the reconciliation does not
  re-derive them afterwards.
