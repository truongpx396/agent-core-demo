.PHONY: model-eval model-eval-all model-eval-baseline model-eval-data model-eval-verify router-label labelset-calibrate test-billing-package help up up-app sandbox-up sandbox-build pull-models pull-strong-model probe-tools ingest index-skills reembed chat chat-hitl serve mcp-serve mcp-serve-ops mcp-gateway-secrets mcp-gateway-up mcp-gateway-bootstrap mcp-gateway-ca mcp-gateway-smoke mcp-gateway-down authlimit-test authlimit-build authlimit-keys authlimit-keys-ensure authlimit-up authlimit-up-app authlimit-down authlimit-down-app authlimit-smoke authlimit-web-check authlimit-latency authlimit-mutants authlimit-revoke telegram telegram-support telegram-sales telegram-person agent-worker agent-worker-support agent-worker-ops agent-worker-sales restart-all fake-llm ingest-worker ops-digest followup-sweep tool-call-dedup-sweep usage-events-sweep usage-events-carry-over billing-migrate billing-migrate-status billing-inbox-sweep billing-export-worker billing-usage-sweep usage-reconcile usage-reconcile-worker budget-policy litellm-key gateway-check web-search-check test test-integration test-live test-sandbox lint typecheck eval promptfoo promptfoo-redteam deepeval garak garak-full trivy trivy-image semgrep checkov sonar-up sonar-down sonar-scan zap-baseline zap-api-scan zap-view defectdojo-up defectdojo-down defectdojo-import loadtest-queued loadtest-queued-headless strix strix-app strix-view logs down clean clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm clear-all obs-up obs-down obs-logs obs-clean

# The billing package is found by path, not installed: the lock file is a machine-generated freeze of a real install and a path
# dependency does not belong in it (spec 011 D20). Absolute, so a target that `cd`s into packages/billing still finds it.
# The billing package and the ML service's `ml_service` package are found by path, not installed (pyproject.toml's pytest `pythonpath` and
# mypy_path do the same for the tools they run). scripts/model_eval imports ml_service to embed with the code the service serves with.
export PYTHONPATH := $(CURDIR)/packages/billing/src:$(CURDIR)/docker/ml-service$(if $(PYTHONPATH),:$(PYTHONPATH))

# The MCP gateway stack's generated secrets (the Casdoor database and admin passwords) live OUTSIDE the repo, in a private directory, and are
# handed to compose as a second --env-file; `.env` is passed first when it exists so every other variable keeps coming from it.
GATEWAY_DIR     := $(HOME)/.cache/graph-agent-core-mcp-gateway
GATEWAY_SECRETS := $(GATEWAY_DIR)/secrets.env

# The authlimit service's internal signing keys (specs/018) also live OUTSIDE the repo, in a private directory created with the host user's
# ownership (so the container, which runs as that user, can read what only its owner may). Never committed, never in an image.
AUTHLIMIT_DIR      := $(HOME)/.cache/graph-agent-core-authlimit
AUTHLIMIT_KEYS_DIR := $(AUTHLIMIT_DIR)/keys

# Every compose file lives under deploy/compose/, but they were written with
# repo-root-relative paths (`./observability/...`, `./postgres-init`,
# `build: .`, `docker/*.Dockerfile`) and read `.env` from the repo root.
# Compose resolves all of those against the PROJECT DIRECTORY, which
# defaults to the directory of the first `-f` file — so without
# `--project-directory .` the paths would resolve under deploy/compose/, the
# root `.env` would be ignored, and the dev stack's project name (it sets no
# `name:`, so it comes from the directory) would silently become `compose`,
# orphaning every existing volume and breaking `sonar-scan`'s network.
# Pinning it here (rather than editing
# every path to `../../`) is also what lets deploy.yml mirror this layout on
# the droplets unchanged. Consequence: a bare `docker compose up` from the
# repo root no longer finds a compose file — use `make up`, or pass the same
# two flags by hand.
COMPOSE     := docker compose --project-directory . -f deploy/compose/docker-compose.yml
COMPOSE_OBS := docker compose --project-directory . -f deploy/compose/docker-compose.observability.yml
# The dev stack's default network is `<project>_default`, and with no `name:` the project is the checkout directory's name (lowercased, characters
# compose rejects dropped; COMPOSE_PROJECT_NAME wins when set). Derived, not typed: a clone or worktree under any name still finds its network.
COMPOSE_NETWORK := $(or $(COMPOSE_PROJECT_NAME),$(shell basename "$(CURDIR)" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-'))_default

# Named explicitly: the included files bring targets of their own.
.DEFAULT_GOAL := help

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# The other targets, by kind.
include make/admin.mk
include make/evals.mk
include make/security.mk
include make/host.mk
include make/test.mk

up:  ## Start all services (litellm, qdrant, langfuse, postgres, minio; talks to a native macOS Ollama on the host, see litellm-config.yaml)
	$(COMPOSE) up -d

up-app:  ## Start infra + the containerized app itself (api, agent-worker, ingest-worker; see Dockerfile)
	$(COMPOSE) --profile app up -d --build

# One compose call over the app and gateway profiles (not sandbox or quality), in identity mode like authlimit-up-app; observability is its own project.
up-all: mcp-gateway-secrets authlimit-keys-ensure  ## Start everything in containers: infra, app behind the front door, MCP gateway, observability (not sandbox or SonarQube); web UI https://api.localhost:8443/
	@curl -sf -o /dev/null http://localhost:11434/api/version || echo "warning: no Ollama on :11434 (start it with: ollama serve)"
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build
	$(COMPOSE_OBS) up -d
	@echo "up. Web UI https://api.localhost:8443/ · Grafana http://localhost:3300 · Langfuse http://localhost:3000"
	@echo "first time: make mcp-gateway-bootstrap, then make mcp-gateway-ca and trust the CA in your browser"

sandbox-up: sandbox-build  ## Start the containerized, authenticated OpenSandbox server (docker/opensandbox-server.{Dockerfile,toml}) that the ops/support/sales domains' sandbox tools execute against — opt-in `sandbox` profile like `up-app`'s `app` profile, not part of plain `make up`, since it bind-mounts the host Docker socket to create sibling sandbox containers. Needs OPENSANDBOX_API_KEY set in .env first (see .env.example) — the server refuses to start without one.
	$(COMPOSE) --profile sandbox up -d --build

sandbox-build:  ## Build the shared sandbox base image (docker/sandbox.Dockerfile — python:3.12-slim + numpy + pandas, non-root, no network egress) that SANDBOX_IMAGE (app/core/config.py) references. Shared by every domain's sandbox tools, not ops-specific. A plain `docker build`, not a docker-compose service — opensandbox-server pulls it by tag from the same host Docker daemon it already has via its bind-mounted socket. Run again after editing that Dockerfile.
	docker build -t graph-agent-core-sandbox:latest -f docker/sandbox.Dockerfile .

pull-models:  ## Download the chat model (qwen2.5:7b, about 4.7 GB), the small model the test tiers default to (qwen2.5:3b, about 1.9 GB) and the embedding model onto the native macOS Ollama this stack talks to (see litellm-config.yaml)
	ollama pull qwen2.5:7b
	ollama pull qwen2.5:3b
	ollama pull nomic-embed-text

pull-strong-model:  ## Download the dev stand-in for the escalation alias (chat-strong): a LOCAL 14B model, about 9 GB (a size up from the 7B chat model). Only needed once ROUTER_MODE is shadow or on
	ollama pull qwen2.5:14b

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m scripts.seed

index-skills:  ## Embed the skills/ catalog (name+description) and upsert into its own Qdrant collection
	python -m scripts.index_skills

reembed:  ## Rebuild a vector collection under the CURRENT embedding identity, then swap it in atomically (specs/014): make reembed COLLECTION_NAME=docs [ARGS=--dry-run]
	# Deliberately NOT `COLLECTION=`: that is also the Settings variable naming the main collection, and make exports command-line
	# variables to the recipe, so `COLLECTION=skills` would rename the main collection for this process and reject `docs`.
	@test -n "$(COLLECTION_NAME)" || { echo "usage: make reembed COLLECTION_NAME=<docs|skills collection> [ARGS=--dry-run]"; exit 2; }
	python -m scripts.reembed $(COLLECTION_NAME) $(ARGS)

# GATEWAY_COMPOSE is defined after COMPOSE (which the command line may override, as the tests of this stack do).
# AUTHLIMIT_UID/GID run the authlimit containers as the host user (see the service in docker-compose.yml); AUTHLIMIT_KEYS_DIR is the key directory.
GATEWAY_ENV = AUTHLIMIT_UID=$$(id -u) AUTHLIMIT_GID=$$(id -g) AUTHLIMIT_KEYS_DIR=$(AUTHLIMIT_KEYS_DIR)
GATEWAY_COMPOSE_CMD = $(COMPOSE) $(if $(wildcard .env),--env-file .env) --env-file $(GATEWAY_SECRETS) --profile gateway
GATEWAY_COMPOSE = $(GATEWAY_ENV) $(GATEWAY_COMPOSE_CMD)

mcp-gateway-secrets:  # internal: create the generated passwords once (never overwritten)
	@mkdir -p $(GATEWAY_DIR) && chmod 700 $(GATEWAY_DIR)
	@test -f $(GATEWAY_SECRETS) || { ( umask 077; printf 'CASDOOR_DB_PASSWORD=%s\nCASDOOR_ADMIN_PASSWORD=%s\n' "$$(openssl rand -hex 16)" "$$(openssl rand -hex 16)" > $(GATEWAY_SECRETS) ) && echo "created $(GATEWAY_SECRETS) (the Casdoor admin password is in it)"; }

authlimit-keys-ensure:  # internal: create the first signing key if there is none (never replaces one)
	@test -f $(AUTHLIMIT_KEYS_DIR)/keyset.json || $(MAKE) --no-print-directory authlimit-keys

mcp-gateway-up: mcp-gateway-secrets authlimit-keys-ensure  ## Start the MCP gateway stack (specs/017): Casdoor + its Postgres, the OAuth-protected gateway and a Caddy with local TLS on https://mcp.localhost:8443. Needs `make up` (the app database). Then `make mcp-gateway-bootstrap`
	$(GATEWAY_COMPOSE) up -d --build --no-deps casdoor-db casdoor mcp-gateway authlimit authlimit-echo caddy-gateway

mcp-gateway-bootstrap: mcp-gateway-secrets  ## Create (idempotently) the Casdoor organizations, applications, scopes and demo users the gateway expects, and print the `claude mcp add` commands. ARGS="--tenant acme --tenant other-co" adds tenants (default: ecorp)
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_COMPOSE) exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.mcp_gateway_bootstrap $(ARGS)

mcp-gateway-ca: mcp-gateway-secrets  ## Export Caddy's local root certificate so a client can trust https://*.localhost:8443 (prints the NODE_EXTRA_CA_CERTS line for Claude Code)
	$(GATEWAY_COMPOSE) cp caddy-gateway:/data/caddy/pki/authorities/local/root.crt $(GATEWAY_DIR)/caddy-root.crt
	@echo "export NODE_EXTRA_CA_CERTS=$(GATEWAY_DIR)/caddy-root.crt   # before starting Claude Code"

mcp-gateway-down: mcp-gateway-secrets  ## Stop and remove the gateway stack's containers (its volumes stay: the Casdoor database and Caddy's local CA)
	$(GATEWAY_COMPOSE) rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db

authlimit-build:  ## Build the authlimit service image (distroless, non-root, no shell)
	docker build -t authlimit:dev services/authlimit

authlimit-keys: authlimit-build  ## Manage the internal signing keys outside the repo. ARGS="init" (default, once) | "rotate --overlap 10m" | "list". Runs as you, so the files are yours (mode 600) and the container, which runs as you too, can read them
	@mkdir -p $(AUTHLIMIT_DIR) && chmod 700 $(AUTHLIMIT_DIR)
	@docker run --rm --user "$$(id -u):$$(id -g)" -v $(AUTHLIMIT_DIR):/work authlimit:dev keys $(or $(ARGS),init) --dir /work/keys

authlimit-up: mcp-gateway-up  ## The authlimit stack: same as mcp-gateway-up (the MCP routes always go through authlimit) - Casdoor, authlimit x2, the MCP gateway, a stub upstream and Caddy on https://{api,mcp,auth}.localhost:8443. Needs `make up` (Redis, Postgres); then `make mcp-gateway-bootstrap`

authlimit-up-app: mcp-gateway-secrets authlimit-keys-ensure  ## The authlimit stack with the REAL API behind the door instead of the echo stub (spec 018): the API in identity mode (it trusts only the signed identity the door adds), one agent worker, Caddy pointed at it. Needs `make up`; then `make mcp-gateway-bootstrap` and `make mcp-gateway-ca`. The web UI is at https://api.localhost:8443/
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build --no-deps casdoor-db casdoor authlimit authlimit-echo mcp-gateway api agent-worker caddy-gateway

authlimit-down-app: mcp-gateway-secrets  ## Stop what authlimit-up-app started, the API and its worker included (the next `--profile app up` brings the API back in header mode)
	$(GATEWAY_COMPOSE) --profile app rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db api agent-worker

authlimit-down: mcp-gateway-down  ## Stop the authlimit stack's containers (same as mcp-gateway-down)

logs:  ## Tail logs from all services
	$(COMPOSE) logs -f

down:  ## Stop all services (keep volumes)
	$(COMPOSE) down

# `down` skips containers started under a profile; `--profile '*'` reaches them all.
down-all: mcp-gateway-secrets  ## Stop and remove every container of the dev stack, any profile, plus observability; keeps volumes
	$(COMPOSE_OBS) down
	$(GATEWAY_COMPOSE) --profile '*' down

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

clear-streams:  ## Delete every Redis Stream (agent:requests:*, agent:results:*, ingest:requests, ingest:results:*) — drops queued/in-flight turns and ingest jobs; leaves the semantic cache and other keys intact
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern '*' | while read -r k; do [ \"\$$(redis-cli type \"\$$k\")\" = stream ] && redis-cli del \"\$$k\"; done"

clear-checkpoints:  ## Truncate the LangGraph checkpointer's tables (checkpoints/checkpoint_blobs/checkpoint_writes) in its dedicated `checkpointer` Postgres DB — drops all saved conversation state; leaves appdata/langfuse/litellm DBs and the migrations tracking table intact
	$(COMPOSE) exec postgres psql -U langfuse -d checkpointer -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"

clear-langfuse:  ## Truncate Langfuse's own telemetry tables (traces, observations incl. generations, scores, trace_sessions, events, comments, media) in the `langfuse` Postgres DB, CASCADE (also clears dependent job_executions rows) — leaves projects/api_keys/users/models/pricing config intact so LANGFUSE_PUBLIC_KEY/SECRET_KEY keep working
	$(COMPOSE) exec postgres psql -U langfuse -d langfuse -c "TRUNCATE traces, observations, scores, trace_sessions, events, comments, media, trace_media, observation_media CASCADE;"

clear-litellm:  ## Truncate LiteLLM's own usage/spend logs (SpendLogs + its tool/guardrail indexes, ErrorLogs, AuditLog, every Daily*Spend/Metrics table) in the `litellm` Postgres DB, CASCADE — leaves users/teams/keys/model config intact so the proxy and its admin UI (`make up` → http://localhost:4000/ui) keep working
	$(COMPOSE) exec postgres psql -U langfuse -d litellm -c "TRUNCATE \"LiteLLM_SpendLogs\", \"LiteLLM_SpendLogToolIndex\", \"LiteLLM_SpendLogGuardrailIndex\", \"LiteLLM_ErrorLogs\", \"LiteLLM_AuditLog\", \"LiteLLM_DailyUserSpend\", \"LiteLLM_DailyTeamSpend\", \"LiteLLM_DailyTagSpend\", \"LiteLLM_DailyEndUserSpend\", \"LiteLLM_DailyOrganizationSpend\", \"LiteLLM_DailyAgentSpend\", \"LiteLLM_DailyToolSpend\", \"LiteLLM_DailyGuardrailMetrics\", \"LiteLLM_DailyPolicyMetrics\" CASCADE;"

clear-all: clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm  ## Run every clear-* target above in one shot — semantic cache, Redis Streams, checkpointer state, Langfuse telemetry, LiteLLM usage/spend logs. Same per-target scope/exclusions as running each individually (see each target's own description); does NOT touch appdata (employees/tickets/leads/incidents/usage_ledger) or delete any volume — for that, `make clean`

clean:  ## Stop services and delete volumes (models, vectors, traces)
	$(COMPOSE) down -v

obs-up:  ## Start the observability stack (Grafana :3300, Prometheus :9090, Loki, Alertmanager, otel-collector) — independent of `make up`
	$(COMPOSE_OBS) up -d

obs-down:  ## Stop the observability stack (keep its volumes)
	$(COMPOSE_OBS) down

obs-logs:  ## Tail logs from the observability stack
	$(COMPOSE_OBS) logs -f

obs-clean:  ## Stop the observability stack and delete its volumes (Prometheus/Loki/Grafana data)
	$(COMPOSE_OBS) down -v
