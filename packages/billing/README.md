# credit-billing

A self-contained prepaid-credit wallet for a SaaS: an append-only, idempotent ledger; signed payment-webhook intake with Stripe and Polar adapters behind one provider port; and operator CLIs. It imports nothing from the application that hosts it, and it ships its own schema, alert rules, dashboard and tests.

The app owns the real-time **entitlement** (what a tenant may still consume); the payment provider owns the **money**. Neither Stripe (credits settle on an invoice) nor Polar (it does not block usage past a balance) can answer "may this request run now?", so the wallet is here ([`docs/PATTERNS.md`](docs/PATTERNS.md), pattern 52).

## Install

```bash
pip install ./packages/billing                 # the wallet, the adapters, the CLIs
pip install "./packages/billing[http]"         # + FastAPI, for the webhook route
pip install "./packages/billing[otel]"         # + the OpenTelemetry SDK and OTLP exporter, so the CLIs can push metrics
pip install "./packages/billing[test]"         # + what the tests need
```

Python 3.11 or newer. Its dependencies are exactly those in [`pyproject.toml`](pyproject.toml); a test fails the build if a module imports anything else.

## Use it from your application

The package never opens a connection or reads your configuration by itself. You give it what it needs once, at process start:

```python
from credit_billing import db

db.configure(connection=my_pool.connection)    # an async context manager yielding a psycopg AsyncConnection
```

The provider **owns the transaction**: one `async with` is one transaction, committed on a clean exit and rolled back when the block raises (psycopg_pool's `pool.connection` does exactly this). Without it the package **refuses** (`BillingNotConfigured`) instead of guessing.

**Schema.** The package ships two idempotent scripts and a tool that applies them to the database you passed above:

```bash
credit-billing-migrate --status    # what is applied, pending or changed; writes nothing
credit-billing-migrate             # apply what is pending or changed, in order, and record it
credit-billing-migrate --check     # exit 1 if anything is pending or changed: a deploy gate
```

Safe to run again (a second run applies nothing), safe to run twice at once (each script is applied once), safe on a database the agent app initialised (re-applying an idempotent script changes no row, and then it is recorded), and it gives up with a clear error instead of waiting for ever when a table is busy (`--lock-timeout-seconds`, default 10). Exit status `0` done or current, `1` `--check` found work, `3` the database could not be reached, `4` a migration did not complete (nothing from that script was applied). It never runs by itself: call it from your deploy, or from your service's start-up with the same connection provider:

```python
from credit_billing import db, migrate
db.configure(connection=pool.connection)
await migrate.migrate()            # a Result(applied=[...], skipped=[...]); MigrationLockTimeout / MigrationFailed on trouble
```

The scripts are *repeatable*: they are idempotent and are edited in place, so a changed checksum means "apply again". They open with psql's `\connect appdata`, a meta-command no driver can run, so `schema.executable(name)` returns the text without that one line (and refuses any other psql meta-command). `22-billing.sql` references tables from `20-credit-wallet.sql`: the numeric order is a dependency.

**Settings** (environment variables or a `.env`; the names are unchanged from the agent app): `BILLING_PROVIDERS` (comma-separated, e.g. `stripe`), `BILLING_WEBHOOK_SECRETS` (JSON, `{"stripe": "whsec_..."}`), `STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`, `BILLING_WEBHOOK_MAX_BODY_BYTES`, `BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE`, `BILLING_WEBHOOK_MAX_ATTEMPTS`, `BILLING_REFUND_HOLD_HOURS`, `BILLING_INBOX_RETENTION_DAYS`. A rejected value is never echoed in the error.

**The two operations an application needs** are the port ([`docs/port.md`](docs/port.md), `credit_billing.port.WalletPort`):

```python
from credit_billing.port import InProcessWallet, Pricing

wallet = InProcessWallet()
# charge a usage event in YOUR transaction (idempotent on event_id; a short balance becomes overdraft, never a refusal)
await wallet.record_usage_in(conn, tenant, amount, event_id=event_id, kind="chat", pricing=Pricing(cost_usd, credits_per_usd, markup))
balance = await wallet.account_balance(tenant)     # None means the tenant has no wallet (so it is not on credit billing)
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
```

The CLIs open their own pool from `APPDATA_DATABASE_URL` (default `postgresql://langfuse:langfuse@localhost:5432/appdata`) and push their metrics to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318`, blank turns it off; needs the `otel` extra). Every change names who and why; a retry is safe only with the same `--key`.

Exit status: `0` done (or a duplicate of a change that already landed), `1` nothing changed (no such wallet), `2` invalid input, `3` the database could not be reached. An unreachable database fails in about ten seconds with one `error:` line naming host, port and database (never the user or password), so a cron job or an operator who mistyped the URL is told at once. Logs go to stderr as one JSON line per record (`timestamp`, `level`, `logger`, `event` plus the record's fields, so the operator audit line `credit_operator_action` carries action, tenant, actor, amount and key); a process that has already configured logging keeps its own setup.

## What it emits and ships for operators

Four counters on the OpenTelemetry **API** (`credit_billing.metrics`: `agent_credit_granted_total`, `agent_credit_debited_total`, `agent_credit_overdraft_total`, `agent_billing_webhook_total`); your process installs the SDK and exporter. The scope is `credit_billing`. [`observability/alerts.yml`](observability/alerts.yml) has the two webhook alerts (`BillingWebhookQuarantined`, `BillingWebhookFailing`) and [`observability/credit-billing-core.json`](observability/credit-billing-core.json) a Grafana dashboard on those counters (it assumes a Prometheus datasource with uid `prometheus`).

## Tests

```bash
cd packages/billing
pytest -q                          # hermetic: no services, no network
pytest -m integration -q           # tests/integration/: a real Postgres via testcontainers, seeded with ONLY the two scripts above (needs Docker)
pytest -m provider_sandbox -q -s   # tests/provider_sandbox/: a provider's real SANDBOX with your own test credentials; manual, never CI, refuses a live key
```

CI installs this package alone into a clean environment and runs the first command with the agent app asserted not importable.

## What it is not (yet)

Not a service: there is no HTTP API beyond the webhook route and no service authentication. Not a billing platform: no invoices, tax, subscriptions management or holds; **holds, API-grade idempotency, outbound notifications and a service-side event store are the next features** ([`docs/design-note.md`](docs/design-note.md), section 6). Usage-based billing to a provider is built but off (prepaid packs only). A reused idempotency key with different parameters currently returns the first result without comparing; that is safe for the application's debits (keyed by a deterministic event id) and is listed as a gap to close before exposing the port over a network.

## Where to read more

[`docs/PATTERNS.md`](docs/PATTERNS.md) (why it is built this way, each with the failure behind it) · [`docs/provider-port.md`](docs/provider-port.md) (what an adapter implements) · [`docs/data-model.md`](docs/data-model.md) · [`docs/port.md`](docs/port.md) · [`docs/boundary.md`](docs/boundary.md) · [`docs/design-note.md`](docs/design-note.md) (turning this into a service).
