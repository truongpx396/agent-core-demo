# Deploying to a Digital Ocean droplet

Provisions TWO droplets (via `deploy/terraform/`):

- The **app droplet**, running a lean production subset of this app's stack
  (`docker-compose.prod.yml`): api + agent-worker + ingest-worker + postgres
  + redis + qdrant + litellm + ml-service, fronted by the same front door as
  the development stack (Caddy on three real names, `authlimit`, Casdoor and
  the MCP gateway; the Telegram bot is opt-in), plus a handful
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
  `ci.yml`'s own `checkov` job) on any PR touching `deploy/terraform/**` —
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
  ssh-keygen -t ed25519 -f ./deploy_key -C "graph-agent-core-ci" -N ""
  ```
  Keep `deploy_key` (private) and `deploy_key.pub` (public) handy for the
  next two steps.

### 2. Provision both droplets

```
cd deploy/terraform
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
sudo mkdir -p /opt/graph-agent-core   # cloud-init already does this; harmless if it exists
cd /opt/graph-agent-core
# paste this repo's deploy/env/prod.env.example content into .env and fill in real
# values (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LLM_API_KEY, MINIO_*,
# CORS_ALLOWED_ORIGINS, APP_IMAGE/ML_IMAGE/AUTHLIMIT_IMAGE, API_DOMAIN/AUTH_DOMAIN/
# MCP_DOMAIN, CASDOOR_DB_PASSWORD/CASDOOR_ADMIN_PASSWORD, OBS_COLLECTOR_ENDPOINT/
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

**Before it, do [step 7](#7-point-dns-at-them-required)** (DNS for the three names: Caddy asks Let's Encrypt for a certificate for each on first
use, and a name that does not resolve yet is retried with backoff and counts against Let's Encrypt's failure limits). **After it, do
[steps 8 and 9](#8-create-authlimits-signing-keys-once)**: until the signing keys exist `authlimit` restarts in a loop and every route that needs
the front door answers 502 (it fails closed); `/health` and the web page stay up, so the deploy's own readiness check passes.

Merge to `main`. Once `CI` passes, `deploy.yml` fires automatically and
runs two independent jobs:
- `deploy`: builds the app + ml-service + authlimit images, pushes them to GHCR,
  rsyncs `deploy/compose/docker-compose.prod.yml`, `deploy/caddy/Caddyfile`
  and `gateway.caddy` (Caddy mounts the directory),
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

**Profiling (specs/022)**: the observability droplet now runs Pyroscope, which the app droplet's api and workers push to on port 4040. The
cloud firewall rule is in `main.tf` (scoped to the VPC, like 4318 and 3100) and `ufw_tcp_ports` lists the port for a droplet provisioned
from now on. On an observability droplet that already exists, open it by hand (`ssh deploy@<observability_ip> sudo ufw allow from
<vpc_cidr> to any port 4040 proto tcp`), run `terraform plan` before applying (an edited `user_data` may be shown as a replacement; this
has not been planned against real state), then set `PYROSCOPE_SERVER_ADDRESS=http://<observability_private_ipv4>:4040` in the app droplet's
`.env` and redeploy. It is empty by default: profiling stays off until you do.

### 6. Mint the app's gateway key

Until you do this the app sends LiteLLM's **master key**, which is gateway admin and cannot carry a
budget, so the gateway has no spend cap of its own. Once the stack is up, mint a scoped,
budget-capped key and give it to the app. The script ships in the `api` image and reaches `litellm`
over the compose network; the master key is passed from your shell's environment, never as an argument:

```
cd /opt/graph-agent-core
export LITELLM_MASTER_KEY="$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key create --max-budget <usd per window> --rpm-limit 600
unset LITELLM_MASTER_KEY
nano .env          # paste the printed key as LITELLM_APP_KEY=
dc up -d           # recreates api / agent-worker / ingest-worker with the new key
```

- The key is printed **once**. `--max-budget` has no default because it is a business number: set it
  **above** the sum of your tenants' own monthly caps, so it fires only when the app-level limits have
  failed (below normal use it turns routine traffic into an outage). It covers chat and embeddings.
- Check it any time with `dc exec -e LITELLM_MASTER_KEY -e LITELLM_APP_KEY api python -m
  scripts.tools.litellm_key info` (spend, budget and the real reset time; LiteLLM aligned `30d` to the next
  month boundary when this was tried, so read `resets at` rather than assuming 30 days).
- When the key is spent, turns fail with `provider_budget_exceeded` and `GatewayBudgetExceeded` pages.
  Find who spent it in LiteLLM's spend logs (grouped by `end_user`, or the `tenant:<name>` tag) before
  raising the budget; `python -m scripts.tools.litellm_key end-user --tenant <name>` maps a name to its id.
- To roll the key, mint a new one, swap `LITELLM_APP_KEY`, `dc up -d`, then delete the old key in
  LiteLLM's UI.
- A key lists the aliases it may call. `create` now defaults to every alias the prod config defines
  (`chat`, `chat-medium`, `chat-strong`, `embed`); a key minted before that lists only `chat,embed` and
  LiteLLM will refuse `chat-strong` or `chat-medium` for it ("key not allowed to access model"). Re-mint
  it, or widen it with LiteLLM's `/key/update`, before turning the router on or attaching a tier.

**The per-person cap, and who a person is.** The app key's budget is one number for the whole app. The
app also sends every call with `user` = `tenant:<tenant>/user:<principal>` (for example
`tenant:acme/user:alice`; a `:` or `/` inside a name is written `%3A` / `%2F`), which LiteLLM keeps as the
call's *end user*: it is what the **Customer Usage** view in the admin UI filters by, so spend can be read
per person (`python -m scripts.tools.litellm_key end-user --tenant acme --principal alice` prints exactly what
the app sends). LiteLLM also holds a *default budget for every end user*, i.e. every person: the configs
name it (`litellm_settings.max_end_user_budget_id: person-default`) and **you create it once**; until you
do, LiteLLM logs "Default end user budget not found in database" on each call and applies no cap at all.

```
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key person-budget set --max-budget <usd per person per window>
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key person-budget info   # exits 1 if it is missing: run it after every deploy
```

- It caps **each person**, not a tenant: a tenant with many people can spend many times it, and a tenant's own
  limits are the app's (`MAX_COST_USD_PER_TENANT_*`, `budget_policies`). There is **no tenant-wide cap at the
  gateway**; that would need the tenant to be the end user, or a key per tenant.
- Size it **above the largest single person's legitimate spend** for the same window, like the key budget: it
  is a backstop beneath the app's limits (`MAX_COST_USD_PER_PRINCIPAL_*` are off by default), not a copy of
  them. `set` again changes it; LiteLLM reads it through a 60-second cache.
- It covers **chat calls only**: embeddings carry no end user yet. A person who reaches it gets the same
  `provider_budget_exceeded` and `GatewayBudgetExceeded` page as the key does; the spend logs' `end_user`
  says whose it was. A different number for one person is a LiteLLM customer with its own budget
  (`/customer/new`, `user_id` = the id above), which duplicates the app's `budget_policies`: prefer those.
- **The end user is readable on purpose, so it is only safe while LiteLLM does not forward `user` to a model
  provider.** Checked against the pinned image (nothing reached a stub backend, on the Ollama and the
  OpenAI-compatible routes); re-run that check whenever the image is bumped (below). If you created the
  earlier `tenant-default` budget, it is no longer referenced: delete it in LiteLLM's UI or with `/budget/delete`.

**Bumping the LiteLLM image.** Both compose files pin `ghcr.io/berriai/litellm:main-stable` by digest, and the two must
stay equal. To move it deliberately:

```
docker pull ghcr.io/berriai/litellm:main-stable
docker image inspect ghcr.io/berriai/litellm:main-stable --format '{{index .RepoDigests 0}}'
docker manifest inspect ghcr.io/berriai/litellm@sha256:<digest>   # must list linux/amd64 AND linux/arm64
```

Put the digest in both `deploy/compose/docker-compose*.yml`, then run **`make gateway-check`** (the new
image on the shipped dev config and a stub model backend; Docker only, about a minute) and `make
probe-tools`, and check a budget stop by hand before shipping. `gateway-check` fails if an alias is
unpriced, if the gateway's cost is not tokens x the configured price, or **if the end user, tenant,
principal, conversation or metadata reaches the model backend**: the end user is readable on purpose, which
is only safe while LiteLLM keeps it to itself, and that is a property of the image, not of this repo. The
stop's HTTP status changed between releases once, and the app's checks were written against 1.94.1.

### 7. Point DNS at them (required)

Create an A (and AAAA, if you use one) record for each of `API_DOMAIN`, `AUTH_DOMAIN` and `MCP_DOMAIN` in the app droplet's `.env`, all pointing at the
`reserved_ip` output (they share one droplet and one Caddy). A domain is **required** for the app droplet: identity needs an https issuer, so there
is no plain-HTTP fallback, and `docker compose` refuses to start without the three names. Caddy requests a real certificate for each automatically on
first use; ports 80 and 443 are already open (`main.tf`'s firewall).

Likewise, if you set `OBS_DOMAIN_NAME` in the observability droplet's `.env`, point another record at `observability_reserved_ip` (that one is still
optional: see `Caddyfile.observability` for its no-domain HTTP-only fallback).

### 8. Create authlimit's signing keys (once)

`authlimit` signs a 30-second identity for the API with an Ed25519 key kept in a host directory outside the repo and the image, mode 700, owned by the
container's user (uid 65532). Make it once, after the first deploy has pulled the image (the `latest` tag is the one just pushed):

```
cd /opt/graph-agent-core
image="$(grep '^AUTHLIMIT_IMAGE=' .env | cut -d= -f2-)"
sudo install -d -m 700 -o 65532 -g 65532 /opt/graph-agent-core/authlimit-keys
docker run --rm --user 65532:65532 -v /opt/graph-agent-core/authlimit-keys:/keys "$image:latest" keys init --dir /keys
dc restart authlimit
dc ps authlimit            # both replicas healthy
```

`keys rotate --overlap 10m` (the same `docker run`, with `keys rotate`) adds a new key, publishes it, and retires the old one after the overlap; the
replicas and the API pick it up without a restart. The directory is a secret: back it up with the `.env`, and never put it in the repo or an image.
If it is lost, run `keys init` again: every replica and the API fetch the new public key, and requests in flight with the old identity (30 seconds) fail once.

### 9. Create the tenants and sign-in clients (once)

Casdoor starts empty. `mcp_gateway_bootstrap` creates, idempotently, one Casdoor organization per tenant, the chat API's public sign-in client for it
(`api-<tenant>`, which the web page, the CLI and the Telegram bot look up by that name), the MCP applications, and one demo user per organization. It runs
inside the MCP gateway container, where Casdoor is `http://casdoor:8000`, and needs the admin password from your shell's environment, never as an argument:

```
cd /opt/graph-agent-core
export CASDOOR_ADMIN_PASSWORD="$(grep '^CASDOOR_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
dc exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.tools.mcp_gateway_bootstrap --tenant ecorp --token-hours 24 \
  [--tenant acme ...] [--telegram-link-url https://<API_DOMAIN>]
unset CASDOOR_ADMIN_PASSWORD
```

- It prints each demo user's password **once**. Those are demonstration accounts: sign in to `https://<AUTH_DOMAIN>` as `built-in/admin`, create your
  real people, and disable the demo ones. A shorter `--token-hours` with a refresh token is the production recommendation (the default is seven
  days; `authlimit revoke` voids a person's tokens at once, see the keys image's `revoke` command).
- `--telegram-link-url` registers the bot's `/telegram/link/callback` as a redirect URI of every tenant's client; leave it out unless the bot links accounts.
- **The admin UI shares the sign-in host.** Anyone can reach `https://<AUTH_DOMAIN>/login`, so the admin password (required, generated) is what protects every
  tenant. Narrowing it by source address is not done: Caddy would need Casdoor's admin paths, which this repo does not enumerate. The sign-in routes are
  charged per address by authlimit (`routes.json` `address_limits`), which slows guessing; it does not stop it, and it does not stop an attacker locking
  an account (Casdoor locks one after five wrong passwords).

**The Telegram bot** is opt-in: set its variables in `.env` (see `prod.env.example`), then `dc --profile telegram up -d telegram-bot`. In `poll` mode (the default) it
needs nothing public; in `webhook` mode Caddy routes `https://<API_DOMAIN>/telegram/webhook` to it, which Telegram calls with the secret you set.

## Everyday operations

```
ssh deploy@<reserved_ip>
cd /opt/graph-agent-core
# Compose files live under deploy/compose/ but resolve their paths and .env
# from this directory, so every call needs --project-directory . (an env var
# can't replace it — COMPOSE_FILE alone makes paths resolve under
# deploy/compose/). The alias saves the typing.
alias dc='docker compose --project-directory . -f deploy/compose/docker-compose.prod.yml'
dc ps
dc logs -f api
dc logs -f authlimit caddy         # the front door: a 429 is a rate limit, a 401 a token, a 502 on a checked route authlimit not ready
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

**Destroying a droplet**: `cd deploy/terraform && terraform destroy` removes
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

1. Apply `postgres-init/19-usage-events.sql` on an existing volume. Skipping it makes every cap read fail:
   under the default `BUDGET_CHECK_FAILURE_POLICY=open` every turn then runs **unchecked** and `TenantAllowanceUnenforced` pages; under `closed` every turn is refused.
2. `make usage-events-carry-over ARGS=--dry-run` and read what it says, then `make usage-events-carry-over`. It copies each `usage_ledger` row older than the first real usage
   event into `usage_events` as `ledger:<id>` (never rated, charged or exported; idempotent; a run that stops at its ceiling is continued by the next), so a monthly cap does not forget the month so far. `ARGS="--tenant acme"` does one tenant.
3. Deploy the release.
4. Run `make usage-events-carry-over` once more. It normally prints `Nothing to carry`; if it carries rows, they were recorded in the gap between steps 2 and 3.

Nothing alerts that step 2 was skipped, so check it: `ARGS=--dry-run` printing `Would carry 0` means the history is whole. There is **no way back by reverting**: since T030c2 nothing writes
the per-turn ledger, so a release that reads it would know spend only up to the day this one deployed. `USAGE_EVENTS_ENABLED=false` no longer exists as a switch (it is refused at startup): the caps would read $0.

## Reconciling the agent's meter with the gateway

The agent compares its own usage events with the LLM gateway's spend log, the one independent record of what was really spent. A third party's meter is the only thing that
notices a call whose event was never written.

```
make usage-reconcile                        # the agent's events against the gateway: 0 agree, 1 drift, 2 could not finish
make usage-reconcile ARGS="--days 7"        # look further back (1-35)
make usage-reconcile ARGS="--json"          # machine-readable
make usage-reconcile-worker                 # a pass every USAGE_RECONCILE_INTERVAL_SECONDS, feeding the gauge and alert
```

The gateway comparison needs the gateway's **admin** key (spend logs are an admin view): `LITELLM_MASTER_KEY` in the environment, never an
argument, and `LITELLM_URL` unless the app's own gateway address resolves (`http://litellm:4000` on the app droplet). Give it to the
`usage-reconcile` container only; the app itself should keep running on `LITELLM_APP_KEY`. The worker refuses to start without the key rather than
quietly skipping the independent meter.

No worker is a compose service yet (disclosed): on the app droplet run them from the app image, which carries `scripts/` and the billing package:

```
# LITELLM_MASTER_KEY is in the droplet's .env; compose reads .env for interpolation only, so export it for this one command
set -a; . ./.env; set +a
dc run -d --no-deps --name usage-reconcile  -e LITELLM_MASTER_KEY api python -m scripts.jobs.usage_reconcile --loop
dc run -d --no-deps --name credit-reconcile api python -m credit_billing.cli.reconcile --loop      # credit billing only
dc run -d --no-deps --name billing-export   api python -m credit_billing.cli.export_worker         # only if a provider bills on usage
```

**Reading a finding.** Each names the tenant, the UTC day and the amounts, and says what it means:

| Finding | Meaning | First step |
|---|---|---|
| `gateway`, drift **above** zero | the gateway spent more than the events record: a call whose event was never written (`UsageEventWriteFailing`), an unpriced call (the note says how many), or spend the app does not meter | check the alert history for that day; `usage_event_unpriced_total`; the pricing of the model. A call with no event is also a call no dollar cap counted |
| `gateway`, drift **below** zero | events with no call behind them, or spend the gateway lost | check the gateway database and its spend-log write queue |
| a tenant you do not recognise, with `gateway` drift above zero | the gateway spent for a tenant with no events at all, the worst shape a lost meter takes (the tenant is read off the end user `tenant:<tenant>/user:<principal>`, so it is named) | check it is not another deployment sharing the gateway (its end users that are not this app's format go to `unattributed`, never here) |
| a tenant named `tenant_<hash> (no tenant in this database hashes to it)` | the same, in a spend-log row written before the end user named the person (the older one-way id) | look the hash up with `python -m scripts.tools.litellm_key end-user --tenant <name>`; these age out with the gateway's spend logs |

**Tolerance.** A difference is drift only above the larger of `USAGE_RECONCILE_TOLERANCE_USD` and `_PCT` of the larger figure. A small
**persistent** gap on every tenant is a price mismatch between the app and the gateway for one of your models, not a loss: tune the percentage
to your own difference rather than ignoring the report. The newest `USAGE_RECONCILE_SETTLE_SECONDS` of traffic is left out (the gateway
writes its spend log in batches, so the newest calls are in the events first).

| Alert | Means | Do |
|---|---|---|
| `UsageReconcileDrift` | the gateway and the agent's events disagree above tolerance on some tenant-day | `make usage-reconcile` |
| `UsageReconcileNotCompleting` | a gateway pass failed, or the gateway had more rows than `USAGE_RECONCILE_GATEWAY_MAX_PAGES` | log `usage_reconcile_failed`; shorten `USAGE_RECONCILE_LOOKBACK_DAYS` or raise the ceiling |
| `UsageEventWriteFailing` | a call was made and its event was not written | reconcile that day against the gateway |

Disclosed gaps: the gauge exists only while `usage-reconcile-worker` runs (a stopped worker **resolves** `UsageReconcileDrift`; cron plus the `make` command still gives the report and the exit code,
not a durable alert; `tests/core/test_gauge_export.py` pins the SDK behaviour this rests on). A pass scans `usage_events` by time while the table's index leads with the tenant (a sequential scan:
fine at moderate volume; a time-only index would tax every model call's insert, so `USAGE_RECONCILE_LOOKBACK_DAYS` is the lever). Embeddings are neither metered nor attributed to a tenant, so their
spend shows as "no tenant of this app", never as drift. A call across midnight UTC is recorded on one day by the gateway (its start) and another by the event (its insert), which shows as a pair of
opposite drifts on adjacent days: that signature is the straddle, not a loss.

## Trimming the usage events

**`make usage-events-sweep`** deletes the agent's usage events older than `USAGE_EVENT_RETENTION_DAYS` (400 by default, **floor 35**: a monthly cap and the reconciliation read back that far). This is the
table spend is read from. It takes no arguments; the window is the setting. It prints what it did and says when it stopped at its ceiling (run it again). Nothing schedules it: run it from cron once your retention
policy is decided. The billing package has its own sweep for its own table (`make billing-usage-sweep`, in the billing [runbook](../packages/billing/docs/RUNBOOK.md)).

## Credit billing (optional)

Everything about credit billing (turning it on, changing a wallet, reconciling the wallet, selling a credit pack with Stripe or Polar, its dashboard and alerts) is the billing package's:
[`packages/billing/docs/RUNBOOK.md`](../packages/billing/docs/RUNBOOK.md). Deployment specifics that are this repository's: apply the package's schema to an existing volume with `make billing-migrate`
(`make billing-migrate-status ARGS=--check` is the deploy gate), before the code that uses it, and start its workers from the app image as shown above.

## Security scanning in front of all this

- **Checkov** (`.checkov.yaml`, CI's `checkov` job, `make checkov`) scans
  `deploy/terraform/**` for IaC misconfiguration before you ever `apply`.
- **Semgrep** (CI's `semgrep` job, `make semgrep`) and **Trivy** (CI's
  `trivy` job) scan the application source/Dockerfile/dependencies that end
  up in the images this workflow ships.
- **SonarQube** (CI's `sonarqube` job, `make sonar-up`/`sonar-scan`) is a
  code-quality/security gate on the same source — see the root README's
  "Static analysis & IaC scanning" section for how all four fit together.

None of these scan the LIVE droplet itself (no DAST here) — they gate what
gets built and shipped to it, not the running instance.
