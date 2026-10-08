# Deploying to a Digital Ocean droplet

Provisions TWO droplets (via `infra/terraform/`):

- The **app droplet**, running a lean production subset of this app's stack
  (`docker-compose.prod.yml`): api + agent-worker + ingest-worker + postgres
  + redis + qdrant + litellm + ml-service, fronted by Caddy, plus a handful
  of lightweight observability sidecars (node-exporter, cadvisor,
  otel-collector-agent, promtail). It deliberately excludes local Ollama,
  Langfuse, open-webui, and MinIO from `docker-compose.yml` — see that
  file's own header comment for the tradeoffs and how to add any of them
  back.
- The **observability droplet**, a separate, smaller box running
  Prometheus/Loki/Grafana/Alertmanager/otel-collector
  (`docker-compose.observability.prod.yml`), fed by the app droplet's
  sidecars above over the DO VPC's private network — never the public
  internet. Kept off the app droplet so a metrics/logs spike can't compete
  with it for CPU/mem. Grafana is the only thing exposed publicly (via its
  own Caddy, real password); Prometheus/Loki/Alertmanager stay private.

Two separate, deliberately non-automatic-together pieces:

- **Infra (Terraform)** — creating/resizing/destroying the droplet itself.
  Always a human running `terraform plan`/`apply` by hand (below), against
  the local state file `versions.tf`'s backend block defaults to. CI's
  `terraform-validate` workflow checks `fmt`/`validate` (and Checkov, via
  `ci.yml`'s own `checkov` job) on any PR touching `infra/terraform/**` —
  structural correctness only, not a real `plan` diff, since a real plan
  needs the actual state file, which a local backend deliberately keeps off
  CI's machine. If you switch to the commented-out DigitalOcean Spaces
  backend in `versions.tf` (recommended for a team), a real `terraform
  plan` step becomes meaningful in CI too and can be added the same way.
- **App deploys (`.github/workflows/deploy.yml`)** — pushing new code to an
  *already-provisioned* droplet. Fully automated: runs after `CI` succeeds
  on `main`, builds+pushes images to GHCR, and SSHes in to pull and restart.

## One-time setup

### 1. Prerequisites

- A DigitalOcean account and an [API token](https://cloud.digitalocean.com/account/api/tokens)
  (read + write).
- An SSH key uploaded to your DO account for your own (human) access:
  `doctl compute ssh-key create my-key --public-key-file ~/.ssh/id_ed25519.pub`,
  then `doctl compute ssh-key list` for its fingerprint.
- A **separate** key pair dedicated to CI deploys — don't reuse your
  personal key:
  ```
  ssh-keygen -t ed25519 -f ./deploy_key -C "agent-core-demo-ci" -N ""
  ```
  Keep `deploy_key` (private) and `deploy_key.pub` (public) handy for the
  next two steps.

### 2. Provision both droplets

```
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in do_token, ssh_key_fingerprints,
                                                # admin_ip_cidrs, deploy_ssh_public_key (deploy_key.pub's contents)
terraform init
terraform plan
terraform apply
```

This provisions both droplets in one apply. Note three outputs:
`reserved_ip` (app droplet's stable address), `observability_reserved_ip`
(observability droplet's stable address), and `observability_private_ipv4`
(its private VPC IP — needed in step 4 below).

### 3. Configure GitHub Actions secrets

Repo Settings -> Secrets and variables -> Actions:

| Secret | Value |
|---|---|
| `DROPLET_HOST` | `terraform output reserved_ip` |
| `OBS_DROPLET_HOST` | `terraform output observability_reserved_ip` |
| `DEPLOY_SSH_KEY` | contents of `deploy_key` (the **private** half from step 1) — authorized on both droplets |

`.github/workflows/deploy.yml` needs nothing else for GHCR — it
authenticates with its own `GITHUB_TOKEN` for the app image, and the
observability stack pulls only public off-the-shelf images.

### 4. Create each droplet's `.env` (once, by hand)

The deploy workflow ships code and config, **never secrets** — it never
writes or touches `.env` on either droplet (see `docker-compose.prod.yml`'s
own header). SSH into each once and create it yourself:

```
ssh deploy@<reserved_ip>
sudo mkdir -p /opt/agent-core-demo   # cloud-init already does this; harmless if it exists
cd /opt/agent-core-demo
# paste this repo's deploy/env/prod.env.example content into .env and fill in real
# values (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LLM_API_KEY, MINIO_*,
# CORS_ALLOWED_ORIGINS, APP_IMAGE/ML_IMAGE, OBS_COLLECTOR_ENDPOINT/
# LOKI_PUSH_HOST from `terraform output observability_private_ipv4`, ...)
# — see that file's own comments.
nano .env
chmod 600 .env
```

```
ssh deploy@<observability_reserved_ip>
sudo mkdir -p /opt/agent-core-observability
cd /opt/agent-core-observability
# paste this repo's deploy/env/observability.prod.env.example content into .env and
# fill in GRAFANA_ADMIN_PASSWORD and ALERTMANAGER_SLACK_WEBHOOK_URL (REQUIRED: the stack
# refuses to start without it, since without a receiver no alert is ever delivered), and
# OBS_DOMAIN_NAME if you're pointing a subdomain at it — see that file's own comments.
nano .env
chmod 600 .env
```

### 5. First deploy

Merge to `main`. Once `CI` passes, `deploy.yml` fires automatically and
runs two independent jobs:
- `deploy`: builds the app + ml-service images, pushes them to GHCR,
  rsyncs `deploy/compose/docker-compose.prod.yml`, `deploy/caddy/Caddyfile`,
  `deploy/litellm/litellm-config.prod.yaml`, `postgres-init/` and
  `observability/` to the app droplet (keeping their repo paths, so the
  droplet mirrors the repo layout), then `docker compose pull && up -d`.
- `deploy-observability`: rsyncs
  `deploy/compose/docker-compose.observability.prod.yml`,
  `deploy/caddy/Caddyfile.observability` and `observability/` to the
  observability droplet, then `docker compose pull && up -d`.

Watch both in the Actions tab, or SSH in and use the `dc` alias from
[Everyday operations](#everyday-operations) below.

**First deploy from the `deploy/` layout**: both jobs also `rm -f` the
pre-`deploy/` flat copies (`docker-compose.prod.yml`, `Caddyfile`,
`litellm-config.prod.yaml`, and the observability pair) once the new `up -d`
has succeeded — a stale flat compose file left in `/opt/...` would still run
and recreate containers from frozen config. Expect exactly
those services whose mount source moved to be recreated once — `caddy` and
`litellm` on the app droplet, `caddy` on the observability droplet (checked
by comparing `docker compose config --hash` before/after the move) — a few
seconds of TLS/gateway blip. Nothing else changes, and the project names,
so the volumes, are unchanged.

**Upgrading an already-provisioned app droplet**: `api` no longer publishes
a fixed host port 8000 (Caddy now load-balances across replicas via a
`dynamic a` upstream instead — see `deploy/caddy/Caddyfile`). `terraform apply` closes
port 8000 at the DO cloud firewall immediately either way (that's the first
enforcement layer), but cloud-init's matching `ufw` rule only applies on
first boot (see `cloud-init.tpl.yaml`'s own comment) — on a droplet
provisioned before this change, either taint+recreate it, or just run `ssh
deploy@<reserved_ip> sudo ufw delete allow 8000/tcp` once by hand; the
latter touches no app data.

### 6. Mint the app's gateway key

Until you do this the app sends LiteLLM's **master key**, which is gateway admin and cannot carry a
budget, so the gateway has no spend cap of its own. Once the stack is up, mint a scoped,
budget-capped key and give it to the app. The script ships in the `api` image and reaches `litellm`
over the compose network; the master key is passed from your shell's environment, never as an argument:

```
cd /opt/agent-core-demo
export LITELLM_MASTER_KEY="$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
dc exec -e LITELLM_MASTER_KEY api python -m scripts.litellm_key create --max-budget <usd per window> --rpm-limit 600
unset LITELLM_MASTER_KEY
nano .env          # paste the printed key as LITELLM_APP_KEY=
dc up -d           # recreates api / agent-worker / ingest-worker with the new key
```

- The key is printed **once**. `--max-budget` has no default because it is a business number: set it
  **above** the sum of your tenants' own monthly caps, so it fires only when the app-level limits have
  failed (below normal use it turns routine traffic into an outage). It covers chat and embeddings.
- Check it any time with `dc exec -e LITELLM_MASTER_KEY -e LITELLM_APP_KEY api python -m
  scripts.litellm_key info` (spend, budget and the real reset time; LiteLLM aligned `30d` to the next
  month boundary when this was tried, so read `resets at` rather than assuming 30 days).
- When the key is spent, turns fail with `provider_budget_exceeded` and `GatewayBudgetExceeded` pages.
  Find who spent it in LiteLLM's spend logs (grouped by `end_user`, or the `tenant:<name>` tag) before
  raising the budget; `python -m scripts.litellm_key end-user --tenant <name>` maps a name to its id.
- To roll the key, mint a new one, swap `LITELLM_APP_KEY`, `dc up -d`, then delete the old key in
  LiteLLM's UI.

### 7. Point DNS at them (optional)

If you set `DOMAIN_NAME` in the app droplet's `.env`, create an A (and
AAAA, if you use one) record pointing it at the `reserved_ip` output.
Likewise, if you set `OBS_DOMAIN_NAME` in the observability droplet's
`.env`, point another record at `observability_reserved_ip`. Caddy (on
each droplet) requests a real cert automatically on first request to its
own host — see `Caddyfile`'s/`Caddyfile.observability`'s own comments for
the no-domain HTTP-only fallback.

## Everyday operations

```
ssh deploy@<reserved_ip>
cd /opt/agent-core-demo
# Compose files live under deploy/compose/ but resolve their paths and .env
# from this directory, so every call needs --project-directory . (an env var
# can't replace it — COMPOSE_FILE alone makes paths resolve under
# deploy/compose/). The alias saves the typing.
alias dc='docker compose --project-directory . -f deploy/compose/docker-compose.prod.yml'
dc ps
dc logs -f api
dc up -d --scale agent-worker=3   # scale workers (GRAPH_PATTERNS.md pattern 43)
```

Observability droplet, separately:

```
ssh deploy@<observability_reserved_ip>
cd /opt/agent-core-observability
alias dc='docker compose --project-directory . -f deploy/compose/docker-compose.observability.prod.yml'
dc ps
dc logs -f grafana
```

Grafana is at `https://<obs_domain>` (or `http://<observability_reserved_ip>`
with no domain set) — log in with `admin` / `GRAFANA_ADMIN_PASSWORD`. If a
dashboard shows no data, check the app droplet's relay first:
`dc logs otel-collector-agent promtail` on the app droplet — both should show successful pushes to the observability
droplet, not connection errors.

**Backups**: `enable_backups` (terraform.tfvars) turns on DO's own weekly
whole-droplet image backups for the **app** droplet only — the simplest
option, off by default. For a finer-grained alternative, a periodic
`pg_dump` of the `postgres` volume's `appdata`/`checkpointer`/`litellm`
databases off-box (e.g. to DO Spaces, the same bucket `MINIO_ENDPOINT`
already points at) captures the actual stateful data without a whole-image
snapshot. The observability droplet is never backed up — it holds metrics/
logs with their own bounded retention (Loki 7d, Prometheus 15d), not data
worth restoring.

**Rollback**: re-run `deploy.yml` against an earlier commit
(`workflow_dispatch` isn't wired up for this file today — re-push/revert the
commit on `main`, or SSH in and `IMAGE_TAG=<older-sha> dc up -d` directly using an older GHCR tag). The
observability stack has no image tags of its own to roll back — its images
are always `:latest` off-the-shelf.

**Destroying a droplet**: `cd infra/terraform && terraform destroy` removes
**both** droplets and both reserved IPs in one go. To remove just the
observability droplet and keep the app running, target it specifically:
`terraform destroy -target=digitalocean_droplet.observability
-target=digitalocean_reserved_ip.observability`. Either way, data in the
app droplet's Docker volumes (postgres/qdrant/redis) is gone with it unless
backed up first (see above); the observability droplet has nothing worth
preserving.

## Upgrading: carry the ledger's history into the usage events

The dollar caps and `GET /usage` now sum `usage_events` (one row per model call) instead of `usage_ledger` (one row per turn), specs/010 T030b. This applies to **every** deployment, with or without
credit billing. The events only have rows from the day `postgres-init/19-usage-events.sql` was applied, so do it in this order:

1. Apply `postgres-init/19-usage-events.sql` on an existing volume (and `21-usage-event-credits.sql` if `CREDITS_PER_USD` is set). Skipping it makes every cap read fail:
   under the default `BUDGET_CHECK_FAILURE_POLICY=open` every turn then runs **unchecked** and `TenantAllowanceUnenforced` pages; under `closed` every turn is refused.
2. `make usage-events-carry-over ARGS=--dry-run` and read what it says, then `make usage-events-carry-over`. It copies each `usage_ledger` row older than the first real usage
   event into `usage_events` as `ledger:<id>` (never rated, charged or exported; idempotent; a run that stops at its ceiling is continued by the next), so a monthly cap does not forget the month so far. `ARGS="--tenant acme"` does one tenant.
3. Deploy the release.
4. Run `make usage-events-carry-over` once more. It normally prints `Nothing to carry`; if it carries rows, they were recorded in the gap between steps 2 and 3.

Nothing alerts that step 2 was skipped, so check it: `ARGS=--dry-run` printing `Would carry 0` means the history is whole. To go back, revert the release: the per-turn ledger is
still written beside the events, so the old read finds it complete. `USAGE_EVENTS_ENABLED=false` no longer exists as a switch (it is refused at startup): the caps would read $0.

## Credit billing: running it

(specs/010-credit-billing-readiness. Everything here is optional: with `CREDITS_PER_USD` unset the whole feature is off and
nothing below applies.)

Three records of the same spending exist, and an operator's job is to know they agree:

| Record | Written by | Read for |
|---|---|---|
| `usage_events`, one row per model call | the app, per call (the billing meter) | what a tenant is charged, **and the dollar caps** |
| `usage_ledger`, one row per turn | the app, per turn | the second record the events are checked against, and the way back |
| the gateway's spend log (LiteLLM, by `end_user`) | the gateway, from the request itself | the independent second meter |

and a fourth question for a tenant with a wallet: was every event worth credits actually debited?

### Turning it on, in the order that cannot hurt

1. Apply the migrations on an existing volume (init scripts run only on a fresh one): `postgres-init/19`..`23`, each with
   `psql -U langfuse -d appdata -f postgres-init/<file>`. Nothing breaks until the next step: no tenant has a wallet.
2. Set `CREDITS_PER_USD` (no default: the price is your decision) and leave `CREDITS_ENFORCEMENT=false`. This is **shadow mode**:
   tenants with a wallet are debited and nobody is refused, so you can watch balances move first (dashboard "Credit Billing").
3. Give a tenant a wallet and credits: `make credits ARGS="grant --tenant acme --amount 500 --by <you> --reason '<why>'"`.
4. Start the two workers below, watch a few days of `make credit-reconcile` come back clean, **then** set `CREDITS_ENFORCEMENT=true`.

### Changing a wallet by hand

```
make credits ARGS="show --tenant acme"                                   # balance, lots, newest ledger entries (read-only)
make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up, ticket 4412'"
make credits ARGS="grant --tenant acme --amount 100 --source promo --expires-in-days 30 --by alice --reason 'launch'"
make credits ARGS="adjust --tenant acme --amount=-12.5 --by alice --reason 'call billed twice'"
```

`--by` and `--reason` are required and stored on the transaction; promo and subscription grants must expire; a negative adjustment can
leave a wallet in debt (a correction is never blocked by the mistake it fixes). **A retry is safe only with the same `--key`**: every
change prints its idempotency key, and re-running the command without it is a second change on purpose.

### The reconciliation

```
make credit-reconcile                       # one pass over today and yesterday (UTC): 0 agree, 1 drift, 2 could not finish
make credit-reconcile ARGS="--days 7"       # look further back (1-35)
make credit-reconcile ARGS="--json"         # machine-readable
make credit-reconcile-worker                # a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, feeding the gauges and alerts
```

The gateway comparison needs the gateway's **admin** key (spend logs are an admin view): `LITELLM_MASTER_KEY` in the environment, never an
argument, and `LITELLM_URL` unless the app's own gateway address resolves (`http://litellm:4000` on the app droplet). Give it to the
reconcile container only; the app itself should keep running on `LITELLM_APP_KEY`. The worker refuses to start without the key rather than
quietly skipping the independent meter (`ARGS=--no-gateway` compares only the app's own records).

Neither worker is a compose service yet (disclosed): on the app droplet run them from the app image, which carries `scripts/`:

```
# LITELLM_MASTER_KEY is in the droplet's .env; compose reads .env for interpolation only, so export it for this one command
set -a; . ./.env; set +a
dc run -d --no-deps --name credit-reconcile -e LITELLM_MASTER_KEY api python -m scripts.credit_reconcile --loop
dc run -d --no-deps --name billing-export    api python -m scripts.billing_export_worker      # only if a provider bills on usage
```

**Reading a finding.** Each names the tenant, the UTC day and the amounts, and says what it means:

| Finding | Meaning | First step |
|---|---|---|
| `gateway`, drift **above** zero | the gateway spent more than the events record: a call whose event was never written (`UsageEventWriteFailing`), an unpriced call (the note says how many), or spend the app does not meter | check the alert history for that day; `usage_event_unpriced_total`; the pricing of the model. A call with no event is also a call no dollar cap counted |
| `gateway`, drift **below** zero | events with no call behind them, or spend the gateway lost | check the gateway database and its spend-log write queue |
| `ledger`, events above the ledger | a turn's ledger write failed (`ledger_write`): the per-turn ledger is missing a turn. The caps read the events, so they were not affected | usually self-evident from the logs; no money was lost and no cap was loose |
| `uncharged` | an event worth credits with no debit: the wallet failed after the meter kept the event (`CreditDebitFailing`) | find the cause first; then repair (below) |
| a tenant named `tenant_<hash> (no tenant in this database hashes to it)` | the gateway spent for someone with no events and no ledger rows at all, the worst shape a lost meter takes | look the hash up with `python -m scripts.litellm_key end-user --tenant <name>`; check it is not another deployment sharing the gateway |

**Repairing an uncharged event, for now by hand.** The debit key IS the event id, so booking an adjustment under that key clears the finding
and can never double-charge: `make credits ARGS="adjust --tenant <t> --amount=-<the event's credits> --key <event_id> --by <you> --reason 'uncharged event <event_id>'"`
(the credits are in `usage_events.credits`; the report lists the count and total per tenant-day). It books as kind `adjust`, by you, not as a `debit`. A repair job is not built (disclosed).

**Tolerance.** A difference is drift only above the larger of `CREDIT_RECONCILE_TOLERANCE_USD` and `_PCT` of the larger figure. A small
**persistent** gap on every tenant is a price mismatch between the app and the gateway for one of your models, not a loss: tune the percentage
to your own difference rather than ignoring the report. The newest `CREDIT_RECONCILE_SETTLE_SECONDS` of traffic is left out (a running turn has
events and no ledger row yet).

### Selling a credit pack with Stripe (sandbox first)

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
   `stripe` CLI logged in to the same sandbox).

### Dashboard and alerts

"Credit Billing" in Grafana (`observability/grafana/dashboards/credit-billing.json`): credits outstanding and owed, grant and debit rates, export
lag, webhook outcomes, the reconciliation's drift and outcomes. There is no Postgres datasource, so who holds what is `make credits ... show`.

| Alert | Means | Do |
|---|---|---|
| `CreditReconcileDrift` | the last pass found a tenant-day above tolerance | `make credit-reconcile` |
| `CreditReconcileNotCompleting` | a pass failed, or the gateway had more rows than `CREDIT_RECONCILE_GATEWAY_MAX_PAGES` | log `credit_reconcile_failed`; shorten `CREDIT_RECONCILE_LOOKBACK_DAYS` or raise the ceiling |
| `CreditDebitFailing` | an event was kept and its wallet debit failed | find why; the next reconciliation names the events |
| `UsageEventWriteFailing` | a call was made and its event was not written | reconcile that day against the gateway |
| `UsageExportStuck` / `Expired` / `Failed` / `EnqueueFailing` | usage is not reaching a provider that bills on it | `usage_export_outbox` (see the alert text) |
| `BillingWebhookQuarantined` / `Failing` | a customer paid and was granted nothing, or applying a payment keeps failing | `billing_webhook_events` |

### What this does not cover (disclosed)

- **The gauges exist only while `credit-reconcile-worker` runs.** A synchronous OpenTelemetry gauge is exported once per set and the collector forgets
  a series five minutes after its last update (both verified; `tests/core/test_gauge_export.py` pins the first), so the worker re-sets them every
  minute. A stopped worker therefore **resolves** `CreditReconcileDrift` rather than leaving it firing, and Prometheus cannot tell a worker that never
  ran from one that died. The same is true of `UsageExportStuck` and the export worker. Cron plus `make credit-reconcile` still gives the report and the
  exit code; it gives the alert nothing durable.
- **A pass scans `usage_events` and `usage_ledger` by time**, and both tables' indexes lead with the tenant (checked with EXPLAIN: a sequential scan). A
  time-only index would make it cheap and would tax the insert of every model call for a job that runs a few times a day, so it was not added. Fine at
  moderate volume; if a pass gets slow, that index (or a partition by day) is the fix and `CREDIT_RECONCILE_LOOKBACK_DAYS` the lever until then.
- **Embeddings** are neither metered nor attributed to a tenant (research G2), so their spend shows as "no tenant of this app", never as drift.
- **A call across midnight UTC** is recorded on one day by the gateway (its start) and another by the event (its insert); an expensive one on a quiet
  tenant shows as a pair of opposite drifts on adjacent days. That signature is the straddle, not a loss.
- **A provider's own balance** is not reconciled: the port has `read_balance` (`BALANCE_READ`) and no adapter declares it yet, so there is nothing to
  compare. It lands with the first adapter that does, through the same contract test.
- **Events that were never queued for export** are alerted by `UsageExportEnqueueFailing` when the failure happens, but the reconciliation does not
  re-derive them afterwards.

## Security scanning in front of all this

- **Checkov** (`.checkov.yaml`, CI's `checkov` job, `make checkov`) scans
  `infra/terraform/**` for IaC misconfiguration before you ever `apply`.
- **Semgrep** (CI's `semgrep` job, `make semgrep`) and **Trivy** (CI's
  `trivy` job) scan the application source/Dockerfile/dependencies that end
  up in the images this workflow ships.
- **SonarQube** (CI's `sonarqube` job, `make sonar-up`/`sonar-scan`) is a
  code-quality/security gate on the same source — see the root README's
  "Static analysis & IaC scanning" section for how all four fit together.

None of these scan the LIVE droplet itself (no DAST here) — they gate what
gets built and shipped to it, not the running instance.
