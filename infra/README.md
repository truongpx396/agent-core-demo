# Deploying to a Digital Ocean droplet

Provisions ONE droplet (via `infra/terraform/`) running a lean production
subset of this app's stack (`docker-compose.prod.yml`): api + agent-worker +
ingest-worker + postgres + redis + qdrant + litellm + ml-service, fronted by
Caddy. It deliberately excludes local Ollama, Langfuse, open-webui, MinIO,
and the observability stack from `docker-compose.yml` — see that file's own
header comment for the tradeoffs and how to add any of them back.

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

### 2. Provision the droplet

```
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in do_token, ssh_key_fingerprints,
                                                # admin_ip_cidrs, deploy_ssh_public_key (deploy_key.pub's contents)
terraform init
terraform plan
terraform apply
```

Note the `reserved_ip` output — that's the droplet's stable address.

### 3. Configure GitHub Actions secrets

Repo Settings -> Secrets and variables -> Actions:

| Secret | Value |
|---|---|
| `DROPLET_HOST` | `terraform output reserved_ip` |
| `DEPLOY_SSH_KEY` | contents of `deploy_key` (the **private** half from step 1) |

`.github/workflows/deploy.yml` needs nothing else — it authenticates to
GHCR with its own `GITHUB_TOKEN`, not a separate registry secret.

### 4. Create the droplet's `.env` (once, by hand)

The deploy workflow ships code and config, **never secrets** — it never
writes or touches `.env` on the droplet (see `docker-compose.prod.yml`'s own
header). SSH in once and create it yourself:

```
ssh deploy@<reserved_ip>
sudo mkdir -p /opt/agent-core-demo   # cloud-init already does this; harmless if it exists
cd /opt/agent-core-demo
# paste this repo's .env.prod.example content into .env and fill in real
# values (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LLM_API_KEY, MINIO_*,
# CORS_ALLOWED_ORIGINS, APP_IMAGE/ML_IMAGE, ...) — see that file's own comments.
nano .env
chmod 600 .env
```

### 5. First deploy

Merge to `main`. Once `CI` passes, `deploy.yml` fires automatically: builds
the app + ml-service images, pushes them to GHCR, rsyncs
`docker-compose.prod.yml`/`Caddyfile`/`litellm-config.prod.yaml`/
`postgres-init/` to the droplet, then `docker compose pull && up -d`.

Watch it in the Actions tab, or SSH in and `docker compose -f
docker-compose.prod.yml ps` / `logs -f api`.

### 6. Point DNS at it (optional)

If you set `DOMAIN_NAME` in `.env`, create an A (and AAAA, if you use one)
record pointing it at the `reserved_ip` output. Caddy requests a real cert
automatically on first request to that host — see `Caddyfile`'s own comment
for the no-domain HTTP-only fallback.

## Everyday operations

```
ssh deploy@<reserved_ip>
cd /opt/agent-core-demo
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml up -d --scale agent-worker=3   # scale workers (GRAPH_PATTERNS.md pattern 43)
```

**Backups**: `enable_backups` (terraform.tfvars) turns on DO's own weekly
whole-droplet image backups — the simplest option, off by default. For a
finer-grained alternative, a periodic `pg_dump` of the `postgres` volume's
`appdata`/`checkpointer`/`litellm` databases off-box (e.g. to DO Spaces,
the same bucket `MINIO_ENDPOINT` already points at) captures the actual
stateful data without a whole-image snapshot.

**Rollback**: re-run `deploy.yml` against an earlier commit
(`workflow_dispatch` isn't wired up for this file today — re-push/revert the
commit on `main`, or SSH in and `IMAGE_TAG=<older-sha> docker compose -f
docker-compose.prod.yml up -d` directly using an older GHCR tag).

**Destroying the droplet**: `cd infra/terraform && terraform destroy` — this
deletes the droplet and reserved IP. Data in its Docker volumes
(postgres/qdrant/redis) is gone with it unless backed up first (see above).

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
