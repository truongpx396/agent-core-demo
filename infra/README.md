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
# paste this repo's .env.prod.example content into .env and fill in real
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
# paste this repo's .env.observability.prod.example content into .env and
# fill in GRAFANA_ADMIN_PASSWORD (and OBS_DOMAIN_NAME if you're pointing a
# subdomain at it) — see that file's own comments.
nano .env
chmod 600 .env
```

### 5. First deploy

Merge to `main`. Once `CI` passes, `deploy.yml` fires automatically and
runs two independent jobs:
- `deploy`: builds the app + ml-service images, pushes them to GHCR,
  rsyncs `docker-compose.prod.yml`/`Caddyfile`/`litellm-config.prod.yaml`/
  `postgres-init/`/`observability/` to the app droplet, then `docker
  compose pull && up -d`.
- `deploy-observability`: rsyncs
  `docker-compose.observability.prod.yml`/`Caddyfile.observability`/
  `observability/` to the observability droplet, then `docker compose pull
  && up -d`.

Watch both in the Actions tab, or SSH in and `docker compose -f
docker-compose.prod.yml ps` / `logs -f api` (app droplet) or `docker
compose -f docker-compose.observability.prod.yml ps` (observability
droplet).

**Upgrading an already-provisioned app droplet**: `api` no longer publishes
a fixed host port 8000 (Caddy now load-balances across replicas via a
`dynamic a` upstream instead — see `Caddyfile`). `terraform apply` closes
port 8000 at the DO cloud firewall immediately either way (that's the first
enforcement layer), but cloud-init's matching `ufw` rule only applies on
first boot (see `cloud-init.tpl.yaml`'s own comment) — on a droplet
provisioned before this change, either taint+recreate it, or just run `ssh
deploy@<reserved_ip> sudo ufw delete allow 8000/tcp` once by hand; the
latter touches no app data.

### 6. Point DNS at them (optional)

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
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml up -d --scale agent-worker=3   # scale workers (GRAPH_PATTERNS.md pattern 43)
```

Observability droplet, separately:

```
ssh deploy@<observability_reserved_ip>
cd /opt/agent-core-observability
docker compose -f docker-compose.observability.prod.yml ps
docker compose -f docker-compose.observability.prod.yml logs -f grafana
```

Grafana is at `https://<obs_domain>` (or `http://<observability_reserved_ip>`
with no domain set) — log in with `admin` / `GRAFANA_ADMIN_PASSWORD`. If a
dashboard shows no data, check the app droplet's relay first:
`docker compose -f docker-compose.prod.yml logs otel-collector-agent
promtail` — both should show successful pushes to the observability
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
commit on `main`, or SSH in and `IMAGE_TAG=<older-sha> docker compose -f
docker-compose.prod.yml up -d` directly using an older GHCR tag). The
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
