.PHONY: model-eval model-eval-all model-eval-baseline model-eval-data model-eval-verify router-label labelset-calibrate test-billing-package help up up-app sandbox-up sandbox-build pull-models pull-strong-model probe-tools ingest index-skills reembed chat chat-hitl serve mcp-serve mcp-serve-ops mcp-gateway-secrets mcp-gateway-up mcp-gateway-bootstrap mcp-gateway-ca mcp-gateway-smoke mcp-gateway-down authlimit-test authlimit-build authlimit-keys authlimit-keys-ensure authlimit-up authlimit-up-app authlimit-down authlimit-down-app authlimit-smoke authlimit-web-check authlimit-latency authlimit-mutants authlimit-revoke telegram telegram-support telegram-sales telegram-person agent-worker agent-worker-all restart-all fake-llm ingest-worker ops-digest followup-sweep tool-call-dedup-sweep usage-events-sweep usage-events-carry-over billing-migrate billing-migrate-status billing-inbox-sweep billing-export-worker billing-usage-sweep usage-reconcile usage-reconcile-worker budget-policy litellm-key gateway-check web-search-check test test-integration test-live test-sandbox lint typecheck openapi openapi-breaking eval promptfoo promptfoo-redteam deepeval garak garak-full trivy trivy-image semgrep checkov sonar-up sonar-down sonar-scan zap-baseline zap-api-scan zap-view defectdojo-up defectdojo-down defectdojo-import loadtest-queued loadtest-queued-headless strix strix-app strix-view logs down clean clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm clear-all obs-up obs-down obs-logs obs-clean collectors-up collectors-down collectors-logs

# The billing package and the ML service's `ml_service` are found by path, not installed: a path dependency does not belong in the
# lock file (spec 011 D20). Absolute, so a target that `cd`s into packages/billing still finds them; pytest and mypy do the same.
export PYTHONPATH := $(CURDIR)/packages/billing/src:$(CURDIR)/services/ml-service$(if $(PYTHONPATH),:$(PYTHONPATH))

# The gateway stack's generated secrets (Casdoor database and admin passwords) live outside the repo and go to compose as a second
# --env-file; `.env` comes first so every other variable still comes from it.
GATEWAY_DIR     := $(HOME)/.cache/graph-agent-core-mcp-gateway
GATEWAY_SECRETS := $(GATEWAY_DIR)/secrets.env

# authlimit's signing keys also live outside the repo, in a directory owned by the host user (the container runs as that user).
# Never committed, never in an image.
AUTHLIMIT_DIR      := $(HOME)/.cache/graph-agent-core-authlimit
AUTHLIMIT_KEYS_DIR := $(AUTHLIMIT_DIR)/keys

# Compose files live under deploy/compose/ but use repo-root paths and read `.env` from the root. `--project-directory .` pins that:
# without it paths resolve under deploy/compose/, `.env` is ignored and the project name silently becomes `compose`, orphaning volumes
# and breaking `sonar-scan`'s network. It also lets deploy.yml mirror this layout. Consequence: a bare `docker compose up` finds no file.
COMPOSE     := docker compose --project-directory . -f deploy/compose/docker-compose.yml
COMPOSE_OBS := docker compose --project-directory . -f deploy/compose/docker-compose.observability.yml
# The collectors file is an overlay on the app's project (the exporters need its network), not a project of its own. COLLECTORS names its
# services, so `up --no-deps` and `rm` touch only them.
COLLECTORS         := otel-collector promtail postgres-exporter redis-exporter pgbouncer-exporter node-exporter cadvisor container-name-mapper
COMPOSE_COLLECTORS := $(COMPOSE) -f deploy/compose/docker-compose.collectors.yml
# Its containers belong to the project but not to docker-compose.yml; without this every `make up` warns about them as orphans.
export COMPOSE_IGNORE_ORPHANS := true
# The default network is `<project>_default`; with no `name:` the project is the checkout directory's name (lowercased, invalid
# characters dropped; COMPOSE_PROJECT_NAME wins). Derived so a clone or worktree under any name finds its network.
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

# One compose call over the app and gateway profiles (not sandbox or quality), in identity mode like authlimit-up-app; observability and its collectors follow.
up-all: mcp-gateway-secrets authlimit-keys-ensure  ## Start everything in containers: infra, app behind the front door, MCP gateway, observability (not sandbox or SonarQube); web UI https://api.localhost:8443/
	@curl -sf -o /dev/null http://localhost:11434/api/version || echo "warning: no Ollama on :11434 (start it with: ollama serve)"
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build
	$(MAKE) obs-up
	@echo "up. Web UI https://api.localhost:8443/ · Grafana http://localhost:3300 · Langfuse http://localhost:3000"
	@echo "first time: make mcp-gateway-bootstrap, then make mcp-gateway-ca and trust the CA in your browser"

sandbox-up: sandbox-build  ## Start the authenticated OpenSandbox server for the domains' sandbox tools (opt-in `sandbox` profile: it mounts the Docker socket). Needs OPENSANDBOX_API_KEY in .env
	$(COMPOSE) --profile sandbox up -d --build

sandbox-build:  ## Build the sandbox base image (deploy/images/sandbox.Dockerfile: python + numpy + pandas, non-root, no network) that SANDBOX_IMAGE references; rerun after editing it
	docker build -t graph-agent-core-sandbox:latest -f deploy/images/sandbox.Dockerfile .

pull-models:  ## Download the chat model (qwen2.5:7b, ~4.7 GB), the small test-tier model (qwen2.5:3b, ~1.9 GB) and the embedding model into the native Ollama
	ollama pull qwen2.5:7b
	ollama pull qwen2.5:3b
	ollama pull nomic-embed-text

pull-strong-model:  ## Download the dev stand-in for the escalation alias chat-strong (local 14B, ~9 GB); only needed once ROUTER_MODE is shadow or on
	ollama pull qwen2.5:14b

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m scripts.tools.seed

index-skills:  ## Embed the skills catalog (name+description) and upsert into its own Qdrant collection
	python -m scripts.tools.index_skills

reembed:  ## Rebuild a vector collection under the CURRENT embedding identity, then swap it in atomically (specs/014): make reembed COLLECTION_NAME=docs [ARGS=--dry-run]
	# Deliberately NOT `COLLECTION=`: that is also the Settings variable naming the main collection, and make exports command-line
	# variables to the recipe, so `COLLECTION=skills` would rename the main collection for this process and reject `docs`.
	@test -n "$(COLLECTION_NAME)" || { echo "usage: make reembed COLLECTION_NAME=<docs|skills collection> [ARGS=--dry-run]"; exit 2; }
	python -m scripts.tools.reembed $(COLLECTION_NAME) $(ARGS)

# Defined after COMPOSE, which the command line may override (the tests of this stack do). AUTHLIMIT_UID/GID run authlimit as the
# host user; AUTHLIMIT_KEYS_DIR is the key directory.
GATEWAY_ENV = AUTHLIMIT_UID=$$(id -u) AUTHLIMIT_GID=$$(id -g) AUTHLIMIT_KEYS_DIR=$(AUTHLIMIT_KEYS_DIR)
GATEWAY_COMPOSE_CMD = $(COMPOSE) $(if $(wildcard .env),--env-file .env) --env-file $(GATEWAY_SECRETS) --profile gateway
GATEWAY_COMPOSE = $(GATEWAY_ENV) $(GATEWAY_COMPOSE_CMD)

mcp-gateway-secrets:  # internal: create the generated passwords once (never overwritten)
	@mkdir -p $(GATEWAY_DIR) && chmod 700 $(GATEWAY_DIR)
	@test -f $(GATEWAY_SECRETS) || { ( umask 077; printf 'CASDOOR_DB_PASSWORD=%s\nCASDOOR_ADMIN_PASSWORD=%s\n' "$$(openssl rand -hex 16)" "$$(openssl rand -hex 16)" > $(GATEWAY_SECRETS) ) && echo "created $(GATEWAY_SECRETS) (the Casdoor admin password is in it)"; }

authlimit-keys-ensure:  # internal: create the first signing key if there is none (never replaces one)
	@test -f $(AUTHLIMIT_KEYS_DIR)/keyset.json || $(MAKE) --no-print-directory authlimit-keys

mcp-gateway-up: mcp-gateway-secrets authlimit-keys-ensure  ## Start the MCP gateway stack (specs/017): Casdoor and its Postgres, the OAuth-protected gateway and Caddy with local TLS on https://mcp.localhost:8443. Needs `make up`; then `make mcp-gateway-bootstrap`
	$(GATEWAY_COMPOSE) up -d --build --no-deps casdoor-db casdoor mcp-gateway authlimit authlimit-echo caddy-gateway

mcp-gateway-bootstrap: mcp-gateway-secrets  ## Create (idempotently) the Casdoor organizations, applications and demo users, and print the `claude mcp add` commands; ARGS="--tenant acme --tenant other-co" adds tenants (default ecorp)
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_COMPOSE) exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.tools.mcp_gateway_bootstrap $(ARGS)

mcp-gateway-ca: mcp-gateway-secrets  ## Export Caddy's local root certificate so a client can trust https://*.localhost:8443 (prints the NODE_EXTRA_CA_CERTS line for Claude Code)
	$(GATEWAY_COMPOSE) cp caddy-gateway:/data/caddy/pki/authorities/local/root.crt $(GATEWAY_DIR)/caddy-root.crt
	@echo "export NODE_EXTRA_CA_CERTS=$(GATEWAY_DIR)/caddy-root.crt   # before starting Claude Code"

mcp-gateway-down: mcp-gateway-secrets  ## Stop and remove the gateway stack's containers (its volumes stay: the Casdoor database and Caddy's local CA)
	$(GATEWAY_COMPOSE) rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db

authlimit-build:  ## Build the authlimit service image (distroless, non-root, no shell)
	docker build -t authlimit:dev services/authlimit

authlimit-keys: authlimit-build  ## Manage the internal signing keys outside the repo: ARGS=init (default, once) | "rotate --overlap 10m" | list. Runs as you, so the key files are yours (mode 600)
	@mkdir -p $(AUTHLIMIT_DIR) && chmod 700 $(AUTHLIMIT_DIR)
	@docker run --rm --user "$$(id -u):$$(id -g)" -v $(AUTHLIMIT_DIR):/work authlimit:dev keys $(or $(ARGS),init) --dir /work/keys

authlimit-up: mcp-gateway-up  ## Alias of mcp-gateway-up: Casdoor, authlimit x2, the MCP gateway, an echo stub and Caddy on https://{api,mcp,auth}.localhost:8443. Needs `make up`, then `make mcp-gateway-bootstrap`

authlimit-up-app: mcp-gateway-secrets authlimit-keys-ensure  ## Like authlimit-up, with the real API (identity mode) and one agent worker behind the door instead of the echo stub (specs/018). Needs `make up`, `make mcp-gateway-bootstrap`, `make mcp-gateway-ca`; web UI https://api.localhost:8443/
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build --no-deps casdoor-db casdoor authlimit authlimit-echo mcp-gateway api agent-worker caddy-gateway

authlimit-down-app: mcp-gateway-secrets  ## Stop what authlimit-up-app started, the API and its worker included (the next `--profile app up` brings the API back in header mode)
	$(GATEWAY_COMPOSE) --profile app rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db api agent-worker

authlimit-down: mcp-gateway-down  ## Stop the authlimit stack's containers (same as mcp-gateway-down)

logs:  ## Tail logs from all services
	$(COMPOSE) logs -f

down:  ## Stop all services (keep volumes), the collectors included
	$(COMPOSE) down --remove-orphans

# `down` skips containers started under a profile; `--profile '*'` reaches them all.
down-all: mcp-gateway-secrets  ## Stop and remove every container of the dev stack, any profile, plus observability and the collectors; keeps volumes
	$(MAKE) obs-down
	$(GATEWAY_COMPOSE) --profile '*' down

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

clear-streams:  ## Delete every Redis Stream (agent:requests:*, agent:results:*, ingest:requests, ingest:results:*) — drops queued/in-flight turns and ingest jobs; leaves the semantic cache and other keys intact
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern '*' | while read -r k; do [ \"\$$(redis-cli type \"\$$k\")\" = stream ] && redis-cli del \"\$$k\"; done"

clear-checkpoints:  ## Truncate the LangGraph checkpointer's tables (checkpoints, checkpoint_blobs, checkpoint_writes): drops all saved conversation state, nothing else
	$(COMPOSE) exec postgres psql -U langfuse -d checkpointer -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"

clear-langfuse:  ## Truncate Langfuse's telemetry tables (traces, observations, scores, sessions, events, comments, media): keeps projects and API keys, so LANGFUSE_*_KEY still works
	$(COMPOSE) exec postgres psql -U langfuse -d langfuse -c "TRUNCATE traces, observations, scores, trace_sessions, events, comments, media, trace_media, observation_media CASCADE;"

clear-litellm:  ## Truncate LiteLLM's usage and spend logs (spend, error and audit logs, the Daily* tables): keeps users, teams, keys and model config, so the proxy and its UI keep working
	$(COMPOSE) exec postgres psql -U langfuse -d litellm -c "TRUNCATE \"LiteLLM_SpendLogs\", \"LiteLLM_SpendLogToolIndex\", \"LiteLLM_SpendLogGuardrailIndex\", \"LiteLLM_ErrorLogs\", \"LiteLLM_AuditLog\", \"LiteLLM_DailyUserSpend\", \"LiteLLM_DailyTeamSpend\", \"LiteLLM_DailyTagSpend\", \"LiteLLM_DailyEndUserSpend\", \"LiteLLM_DailyOrganizationSpend\", \"LiteLLM_DailyAgentSpend\", \"LiteLLM_DailyToolSpend\", \"LiteLLM_DailyGuardrailMetrics\", \"LiteLLM_DailyPolicyMetrics\" CASCADE;"

clear-all: clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm  ## Run every clear-* target (cache, Redis Streams, checkpointer, Langfuse, LiteLLM logs). Leaves appdata and volumes; `make clean` deletes volumes

clean:  ## Stop services and delete volumes (models, vectors, traces)
	$(COMPOSE) down -v --remove-orphans

obs-up:  ## Start observability (Grafana :3300, Prometheus :9090, Loki, Alertmanager, Pyroscope) and the collectors that feed it; independent of `make up`
	$(COMPOSE_OBS) up -d
	$(MAKE) collectors-up

obs-down:  ## Stop observability and the collectors (keep volumes)
	$(COMPOSE_OBS) down
	$(MAKE) collectors-down

obs-logs:  ## Tail logs from the observability stack
	$(COMPOSE_OBS) logs -f

obs-clean:  ## Stop observability and the collectors and delete the observability volumes (Prometheus/Loki/Grafana data)
	$(COMPOSE_OBS) down -v
	$(MAKE) collectors-down

collectors-up:  ## Start the telemetry collectors (OTel collector, exporters, cAdvisor, Promtail) beside `make up`; Prometheus and Loki (`make obs-up`) read them
	$(COMPOSE_COLLECTORS) up -d --no-deps $(COLLECTORS)

collectors-down:  ## Stop and remove the telemetry collectors
	$(COMPOSE_COLLECTORS) rm -sf $(COLLECTORS)

collectors-logs:  ## Tail logs from the telemetry collectors
	$(COMPOSE_COLLECTORS) logs -f $(COLLECTORS)
