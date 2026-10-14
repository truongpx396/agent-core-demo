# credit-billing

A self-contained prepaid-credit wallet for a SaaS: an append-only, idempotent ledger; signed payment-webhook intake with Stripe and Polar adapters behind one provider port; the **billing half of a usage meter** (rate a unit of usage in credits, keep a record of it, debit the wallet, queue it for a usage-billing provider, all in your transaction); the credit gate; and operator CLIs. It imports nothing from the application that hosts it, and it ships its own schema, alert rules, dashboard and tests.

The app owns the real-time **entitlement** (what a tenant may still consume); the payment provider owns the **money**. Neither Stripe (credits settle on an invoice) nor Polar (it does not block usage past a balance) can answer "may this request run now?", so the wallet is here ([`docs/PATTERNS.md`](docs/PATTERNS.md), pattern 52).

## Install

```bash
pip install ./packages/billing                 # the wallet, the adapters, the CLIs
pip install "./packages/billing[http]"         # + FastAPI, for the webhook route
pip install "./packages/billing[otel]"         # + the OpenTelemetry SDK and OTLP exporter, so the CLIs can push metrics
pip install "./packages/billing[test]"         # + what the tests need
```

Python 3.11 or newer. Its dependencies are exactly those in [`pyproject.toml`](pyproject.toml); a test fails the build if a module imports anything else.

## Minimal setup in another project

Five steps, no billing code of your own:

```bash
pip install "./packages/billing[http,otel]"
export APPDATA_DATABASE_URL=postgresql://...        # the database your application already uses
export CREDITS_PER_USD=1000 MARKUP=1                 # your price: no default is shipped, on purpose
export BILLING_PROVIDERS=stripe BILLING_WEBHOOK_SECRETS='{"stripe":"whsec_..."}' STRIPE_API_KEY=sk_test_...
credit-billing-migrate                               # creates the wallet, the webhook inbox, the usage-event store and the export outbox
```

```python
from credit_billing import db, http
from credit_billing.port import InProcessBilling, Usage

db.configure(connection=pool.connection)             # once, at start
app.include_router(http.router); http.validate_configuration()    # payments in: POST /billing/webhooks/{provider}
billing = InProcessBilling()

# wherever your own meter records a unit of usage, in the SAME transaction as that record:
await billing.record_usage_in(conn, Usage(tenant="acme", event_id="call-123", kind="chat", cost_usd=0.0123, model="gpt", tokens=150))

# before starting work for a tenant (reads nothing unless CREDITS_ENFORCEMENT=true):
answer = await billing.admit("acme", reserved_usd=lambda: my_inflight_dollars("acme"))
if answer.refused: ...                                # answer.status is "exhausted" or "unavailable"
```

That is the whole integration. `record_usage_in` rates the cost (`credits = round_half_up(cost_usd x CREDITS_PER_USD x MARKUP, 6)`), keeps an append-only record keyed `(tenant, event_id)`, debits the tenant's wallet idempotently (a short balance becomes overdraft, never a refusal), and queues the usage for any enabled provider that bills on it. It never raises for an operational fault: every way it can fail is counted and has an alert in [`observability/alerts.yml`](observability/alerts.yml). Run the workers you need: `credit-billing-export-worker` (only with a usage-billing provider), `credit-billing-reconcile --loop` (was every usage charged?), and the sweeps `credit-billing-usage-sweep` and `credit-billing-inbox-sweep` from cron.

Two things the caller owns: **`event_id` must be stable across retries** of the same unit of usage (so a replay is one charge) and **unique across your tenants if you export to a provider** (the provider deduplicates on it alone); and **the cost is yours** (only you know your model's price). Nothing else about your meter has to look like ours: the agent app keeps an LLM-shaped table for its spend caps and hands this package six plain fields.

## Use it from your application (the details)

The package never opens a connection or reads your configuration by itself. You give it what it needs once, at process start:

```python
from credit_billing import db

db.configure(connection=my_pool.connection)    # an async context manager yielding a psycopg AsyncConnection
```

The provider **owns the transaction**: one `async with` is one transaction, committed on a clean exit and rolled back when the block raises (psycopg_pool's `pool.connection` does exactly this). Without it the package **refuses** (`BillingNotConfigured`) instead of guessing.

**Schema.** The package ships four idempotent scripts (`20-` the wallet, `22-` the customer link, product catalog and webhook inbox, `24-` the usage-event store, `25-` the export outbox) and a tool that applies them to the database you passed above:

```bash
credit-billing-migrate --status    # what is applied, pending or changed; writes nothing
credit-billing-migrate             # apply what is pending or changed, in order, and record it
credit-billing-migrate --check     # exit 1 if anything is pending or changed: a deploy gate
```

Safe to run again (a second run applies nothing), safe to run twice at once (each script is applied once), safe on a database the agent app initialised (re-applying an idempotent script changes no row, and then it is recorded), and it gives up with a clear error instead of waiting for ever when a table is busy (`--lock-timeout-seconds`, default 10). Exit status `0` done or current, `1` `--check` found work, `3` the database could not be reached, `4` a migration did not complete (nothing from that script was applied). **Run it BEFORE deploying a version that needs new schema** (spec 012 added `credit_transactions.request`; spec 013 added the two usage tables): a missing column fails every keyed wallet write, and a missing usage table is counted and paged (`BillingEventWriteFailing`) rather than fatal, but the usage is not recorded meanwhile. `--check` is the gate for that. It never runs by itself: call it from your deploy, or from your service's start-up with the same connection provider:

```python
from credit_billing import db, migrate
db.configure(connection=pool.connection)
await migrate.migrate()            # a Result(applied=[...], skipped=[...]); MigrationLockTimeout / MigrationFailed on trouble
```

The scripts are *repeatable*: they are idempotent and are edited in place, so a changed checksum means "apply again". They open with psql's `\connect appdata`, a meta-command no driver can run, so `schema.executable(name)` returns the text without that one line (and refuses any other psql meta-command). `22-billing.sql` references tables from `20-credit-wallet.sql`: the numeric order is a dependency.

**Settings** (environment variables or a `.env`; the names are unchanged from the agent app): money in: `BILLING_PROVIDERS` (comma-separated, e.g. `stripe`), `BILLING_WEBHOOK_SECRETS` (JSON, `{"stripe": "whsec_..."}`), `STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`, `BILLING_WEBHOOK_MAX_BODY_BYTES`, `BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE`, `BILLING_WEBHOOK_MAX_ATTEMPTS`, `BILLING_REFUND_HOLD_HOURS`, `BILLING_INBOX_RETENTION_DAYS`; price and gate: `CREDITS_PER_USD` (no default: unset means nothing is rated, debited or gated), `MARKUP`, `CREDITS_ENFORCEMENT`, `CREDIT_CHECK_FAILURE_POLICY`; money out: `BILLING_EXPORT_BATCH_SIZE`, `_INTERVAL_SECONDS`, `_MAX_ATTEMPTS`, `_BACKOFF_BASE_SECONDS`, `_BACKOFF_CAP_SECONDS`, `_MAX_AGE_DAYS`, `_CALL_TIMEOUT_SECONDS`; the checks: `CREDIT_RECONCILE_SETTLE_SECONDS`, `_LOOKBACK_DAYS`, `_INTERVAL_SECONDS`, `BILLING_USAGE_EVENT_RETENTION_DAYS`. A rejected value is never echoed in the error.

**The four operations an application needs** are the port ([`docs/port.md`](docs/port.md), `credit_billing.port.BillingPort`):

```python
billing = InProcessBilling()
recorded = await billing.record_usage_in(conn, Usage(tenant, event_id, "chat", cost_usd, model="gpt", tokens=150))   # rate, record, debit, queue: in YOUR transaction
admission = await billing.admit(tenant, reserved_usd=reader)    # the credit gate: allowed | exhausted | unavailable | degraded
summary = await billing.credit_summary(tenant)                  # what to show the tenant: balance + whether the gate enforces, or None
balance = await billing.account_balance(tenant)                 # None means the tenant has no wallet (so it is not on credit billing)
```

**The webhook route.** `credit_billing.http.router` serves `POST /billing/webhooks/{provider}`: signature first, then the inbox, then the wallet, answering 2xx only after the commit. Mount it and validate the configuration at startup (this also creates the metric series the alerts watch):

```python
from credit_billing import http
app.include_router(http.router)
http.validate_configuration()        # raises, so the process refuses to start, for a provider with no adapter or no secret
```

Per-source rate limiting is yours to add in front of `/billing/webhooks/`; the package exposes the limit as a setting only.

## Operate it

```bash
credit-billing grant  --tenant acme --amount 500 --by alice --reason "pilot top-up, ticket 4412"
credit-billing adjust --tenant acme --amount -12.5 --by alice --reason "call billed twice"
credit-billing show   --tenant acme --entries 40
credit-billing-inbox-sweep           # drop applied/ignored webhook-inbox rows past BILLING_INBOX_RETENTION_DAYS
credit-billing-usage-sweep           # drop usage events past BILLING_USAGE_EVENT_RETENTION_DAYS, never one whose export never finished
credit-billing-reconcile             # was every usage worth credits charged? exit 0 yes / 1 drift / 2 could not finish / 3 database unreachable; --loop is the worker
credit-billing-export-worker         # drain the export outbox to usage-billing providers (--once for one pass); exits at once if none is enabled
```

The CLIs open their own pool from `APPDATA_DATABASE_URL` (default `postgresql://langfuse:langfuse@localhost:5432/appdata`) and push their metrics to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318`, blank turns it off; needs the `otel` extra). Every change names who and why; a retry is safe only with the same `--key`.

A key stands for one request: the same `--key` with something different (another amount, source, reason or expiry) is refused with exit status `2`, one line per differing field, and nothing changed. `--expires-in-days` is recomputed on every run, so for an exact retry give the first run's instant as `--expires-at 2026-11-13T07:20:11+00:00` (the conflict message prints it); the two cannot be combined.

Exit status: `0` done (or a duplicate of a change that already landed), `1` nothing changed (no such wallet), `2` invalid input (including a reused key with a different request), `3` the database could not be reached. An unreachable database fails in about ten seconds with one `error:` line naming host, port and database (never the user or password), so a cron job or an operator who mistyped the URL is told at once. Logs go to stderr as one JSON line per record (`timestamp`, `level`, `logger`, `event` plus the record's fields, so the operator audit line `credit_operator_action` carries action, tenant, actor, amount and key); a process that has already configured logging keeps its own setup.

## What it emits and ships for operators

Counters and gauges on the OpenTelemetry **API** (`credit_billing.metrics`: the wallet's `agent_credit_granted_total`, `agent_credit_debited_total`, `agent_credit_overdraft_total`; the webhook's `agent_billing_webhook_total`; the gate's `agent_credit_enforcement_refused_total`; the export's `agent_usage_export_total` and `agent_usage_export_oldest_pending_age_seconds`; the wallet check's `agent_credit_reconcile_total`, `agent_credit_reconcile_max_drift_usd` and `agent_credit_outstanding`; and `agent_cost_governance_degraded_total{path}` for `billing_event_write`, `credit_debit`, `export_enqueue` and `credit_read`); your process installs the SDK and exporter. The scope is `credit_billing`. [`observability/alerts.yml`](observability/alerts.yml) has an alert for every way billing can fail without anyone noticing (eleven: the two webhook ones, an unrecorded usage, an uncharged one, four for the export, the gate, and the wallet check) and [`observability/credit-billing-core.json`](observability/credit-billing-core.json) a Grafana dashboard on them (it assumes a Prometheus datasource with uid `prometheus`). The metric names keep their `agent_` prefix: renaming them is a service feature, with a migration ([`docs/ROADMAP.md`](docs/ROADMAP.md)).

## Tests

```bash
cd packages/billing
pytest -q                          # hermetic: no services, no network
pytest -m integration -q           # tests/integration/: a real Postgres via testcontainers, seeded with ONLY the scripts the package ships (needs Docker)
pytest -m provider_sandbox -q -s   # tests/provider_sandbox/: a provider's real SANDBOX with your own test credentials; manual, never CI, refuses a live key
```

CI installs this package alone into a clean environment and runs the first command with the agent app asserted not importable.

## What it is not (yet)

Not a service: there is no HTTP API beyond the webhook route and no service authentication. Not a billing platform: no invoices, tax, subscriptions management or holds; **holds and outbound notifications are the next wallet features, then the service shell and turnkey setup** (the open work, in order, with where each item's reasoning lives, is [`docs/ROADMAP.md`](docs/ROADMAP.md)). The usage-event store is in-process (it takes your connection and your transaction) and is the shape a service would take over, not yet a service of its own. Usage-based billing to a provider is built but off (prepaid packs only). A reused idempotency key with a different request is refused (`IdempotencyConflict`), not answered "already applied". One limit: a row written before the `request` column existed has no stored request and keeps the old behaviour for ever.

## Where to read more

[`docs/ROADMAP.md`](docs/ROADMAP.md) (what is not built, in order, and every known limit) · [`docs/PATTERNS.md`](docs/PATTERNS.md) (why it is built this way, each with the failure behind it) · [`docs/provider-port.md`](docs/provider-port.md) (what an adapter implements) · [`docs/data-model.md`](docs/data-model.md) · [`docs/port.md`](docs/port.md) · [`docs/boundary.md`](docs/boundary.md) · [`docs/design-note.md`](docs/design-note.md) (turning this into a service).
