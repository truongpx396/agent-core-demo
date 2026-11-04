.PHONY: model-eval model-eval-all model-eval-baseline model-eval-data model-eval-verify router-label labelset-calibrate test-billing-package help up up-app sandbox-up sandbox-build pull-models pull-strong-model probe-tools ingest index-skills reembed chat chat-hitl serve mcp-serve mcp-serve-ops mcp-gateway-secrets mcp-gateway-up mcp-gateway-bootstrap mcp-gateway-ca mcp-gateway-smoke mcp-gateway-down authlimit-test authlimit-build authlimit-keys authlimit-keys-ensure authlimit-up authlimit-up-app authlimit-down authlimit-down-app authlimit-smoke authlimit-web-check authlimit-latency authlimit-mutants authlimit-revoke telegram telegram-support telegram-sales telegram-person agent-worker agent-worker-support agent-worker-ops agent-worker-sales restart-all fake-llm ingest-worker ops-digest followup-sweep tool-call-dedup-sweep usage-events-sweep usage-events-carry-over billing-migrate billing-migrate-status billing-inbox-sweep billing-export-worker billing-usage-sweep usage-reconcile usage-reconcile-worker budget-policy litellm-key gateway-check web-search-check test test-integration test-live test-sandbox lint typecheck eval promptfoo promptfoo-redteam deepeval garak garak-full trivy trivy-image semgrep checkov sonar-up sonar-down sonar-scan zap-baseline zap-api-scan zap-view defectdojo-up defectdojo-down defectdojo-import loadtest-queued loadtest-queued-headless strix strix-app strix-view logs down clean clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm clear-all obs-up obs-down obs-logs obs-clean

# The billing package and the ML service's `ml_service` are found by path, not installed: a path dependency does not belong in the
# lock file (spec 011 D20). Absolute, so a target that `cd`s into packages/billing still finds them; pytest and mypy do the same.
export PYTHONPATH := $(CURDIR)/packages/billing/src:$(CURDIR)/docker/ml-service$(if $(PYTHONPATH),:$(PYTHONPATH))

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

sandbox-up: sandbox-build  ## Start the authenticated OpenSandbox server for the domains' sandbox tools (opt-in `sandbox` profile: it mounts the Docker socket). Needs OPENSANDBOX_API_KEY in .env
	$(COMPOSE) --profile sandbox up -d --build

sandbox-build:  ## Build the sandbox base image (docker/sandbox.Dockerfile: python + numpy + pandas, non-root, no network) that SANDBOX_IMAGE references; rerun after editing it
	docker build -t graph-agent-core-sandbox:latest -f docker/sandbox.Dockerfile .

pull-models:  ## Download the chat model (qwen2.5:7b, ~4.7 GB), the small test-tier model (qwen2.5:3b, ~1.9 GB) and the embedding model into the native Ollama
	ollama pull qwen2.5:7b
	ollama pull qwen2.5:3b
	ollama pull nomic-embed-text

pull-strong-model:  ## Download the dev stand-in for the escalation alias chat-strong (local 14B, ~9 GB); only needed once ROUTER_MODE is shadow or on
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
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_COMPOSE) exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.mcp_gateway_bootstrap $(ARGS)

mcp-gateway-ca: mcp-gateway-secrets  ## Export Caddy's local root certificate so a client can trust https://*.localhost:8443 (prints the NODE_EXTRA_CA_CERTS line for Claude Code)
	$(GATEWAY_COMPOSE) cp caddy-gateway:/data/caddy/pki/authorities/local/root.crt $(GATEWAY_DIR)/caddy-root.crt
	@echo "export NODE_EXTRA_CA_CERTS=$(GATEWAY_DIR)/caddy-root.crt   # before starting Claude Code"

mcp-gateway-smoke: mcp-gateway-secrets  ## The OAuth + MCP end-to-end check against the running gateway stack, as a client does it (throwaway users, deleted after). Run it after `make mcp-gateway-ca` and `make mcp-gateway-bootstrap`, and after any change to the Casdoor digest. A manual tier, like test-sandbox: never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.mcp_gateway_smoke --ca $(GATEWAY_DIR)/caddy-root.crt

mcp-gateway-down: mcp-gateway-secrets  ## Stop and remove the gateway stack's containers (its volumes stay: the Casdoor database and Caddy's local CA)
	$(GATEWAY_COMPOSE) rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db

authlimit-test:  ## Go vet + the authlimit service's own tests with the race detector (specs/018; no Docker, no services; needs Go)
	cd services/authlimit && go vet ./... && go test -race ./...

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

authlimit-smoke: mcp-gateway-secrets  ## The end-to-end check of the front door as a client sees it: sign-in, refusal, rate limit, revocation, replica kill, Casdoor outage, key rotation (throwaway users, deleted after). Run after `make authlimit-up`, `make mcp-gateway-ca` and `make mcp-gateway-bootstrap`. A manual tier like mcp-gateway-smoke: never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_ENV) python -m scripts.authlimit_smoke --ca $(GATEWAY_DIR)/caddy-root.crt --keys-dir $(AUTHLIMIT_KEYS_DIR) --compose '$(GATEWAY_COMPOSE_CMD)' $(ARGS)

authlimit-web-check:  ## The built-in web page in a REAL browser (Playwright/Chromium): sign in through Casdoor's own form, chat, see only your tenant's rows, sign out, and what a refused token does (specs/018, two tenants). Run after `make authlimit-up-app`, `make mcp-gateway-bootstrap ARGS="--tenant ecorp --tenant other-co"` and `make mcp-gateway-ca`, with the worker on loadtest/fake_llm_server.py. A manual tier like authlimit-smoke: never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.authlimit_web_check --ca $(GATEWAY_DIR)/caddy-root.crt $(ARGS)

authlimit-latency:  ## How much time the front door's check adds to a request: a checked route against a public one, same connection, alternating (specs/018 SC-008). Run after `make authlimit-up` and `make mcp-gateway-bootstrap`. ARGS="-n 1000 --path /chat/sessions"
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.authlimit_latency --ca $(GATEWAY_DIR)/caddy-root.crt $(ARGS)

authlimit-mutants:  ## Apply each deliberate defect of the authlimit service's mutation list and require a test to fail (specs/018 SC-010). Edits the source in place and restores it; run on a clean tree. Needs Go
	python scripts/authlimit_mutants.py

gateway-check:  ## Run the PINNED LiteLLM image on the shipped dev config and a stub model backend: every alias priced, spend equals tokens x price, and nothing identifying (the readable end user, tenant, principal, conversation) reaches the backend. Needs Docker, no model, key or database. Re-run after bumping the image (infra/README.md)
	python -m scripts.gateway_check

web-search-check:  ## Does SERPER_API_KEY work? One real search through the web_search tool's own client; spends ONE serper.dev credit, prints the outcome and the first result, never the key. Exit 0 ok, 1 refused/failed, 2 no key (GRAPH_PATTERNS.md pattern 87)
	python -m scripts.web_search_check

test:  ## Run the graph test suite in parallel (no live services needed — fake LLM, no Qdrant), then the billing package's own tests (a separate process: both have a `tests` package)
	pytest -n auto -q
	cd packages/billing && pytest -q

test-billing-package:  ## The billing package's own hermetic tests, alone, from its directory: the agent app is not on the path (spec 011)
	cd packages/billing && pytest -q

test-integration:  ## Real Postgres/Redis/Qdrant/ml-service/crawl4ai via testcontainers (no LLM) — needs Docker, no `make up` required (GRAPH_PATTERNS.md pattern 48/50)
	# --dist=loadgroup: tests/integration/test_worker_scaling.py's own
	# xdist_group marker needs this to actually take effect (plain `load`
	# ignores it) — see that module's own comment for why.
	pytest -n auto -m integration -q --dist=loadgroup
	cd packages/billing && pytest -m integration -q

test-live:  ## Real small Ollama model + ml-service + full app/agent-worker stack via testcontainers, incl. Playwright browser E2E and real crawl4ai renders — needs Docker (pattern 48/50)
	playwright install --with-deps chromium
	pytest -n auto -m "llm or e2e or crawl" -q

test-sandbox:  ## Real OpenSandbox MCP round trip (pattern 50) — needs `make sandbox-up` running separately AND opensandbox-mcp on PATH; self-skips cleanly if either isn't there. Deliberately manual, like `make deepeval`/`garak` — never CI
	pytest -m sandbox -q -s

.PHONY: test-provider-sandbox
test-provider-sandbox:  ## A payment provider's real SANDBOX (specs/010 T029): real Checkout requests and REAL signed webhook deliveries via `stripe listen` / `polar listen` (Stripe, Polar). Needs STRIPE_API_KEY (test-mode) and/or POLAR_ACCESS_TOKEN (POLAR_ENVIRONMENT=sandbox) in .env plus the provider's CLI; self-skips without them or beside your own `listen`, refuses a live key. Creates labelled objects in the sandbox. Deliberately manual — never CI
	pytest -c packages/billing/pyproject.toml packages/billing/tests -m provider_sandbox -q -s -p no:xdist

lint:  ## Static checks: ruff (style/correctness) — see pyproject.toml's [tool.ruff]
	ruff check .

typecheck:  ## Static checks: mypy over app/ and scripts/ — see pyproject.toml's [tool.mypy]
	mypy

loadtest-up:  ## The one command to run before `make loadtest-queued`/`-headless`: (re)starts loadtest/fake_llm_server.py as a backgrounded host process (var/fake-llm.log) and points the containerized api/agent-worker*/ingest-worker at it (`loadtest-app-up`). Safe to re-run any time — kills and waits out any already-running fake-llm first, same idempotent-restart idiom as `restart-all`. Counterpart: `make loadtest-down`.
	pkill -f 'loadtest.fake_llm_server' 2>/dev/null || true
	for i in $$(seq 1 20); do \
		pgrep -f 'loadtest.fake_llm_server' >/dev/null 2>&1 || break; \
		sleep 0.5; \
	done
	mkdir -p var
	nohup $(MAKE) fake-llm > var/fake-llm.log 2>&1 &
	for i in $$(seq 1 20); do \
		curl -sf http://localhost:9009/health >/dev/null 2>&1 && break; \
		sleep 0.5; \
	done
	$(MAKE) loadtest-app-up

loadtest-down:  ## Counterpart to `make loadtest-up` — stops the backgrounded fake_llm_server.py and points api/agent-worker*/ingest-worker back at real litellm (`make up-app`).
	pkill -f 'loadtest.fake_llm_server' 2>/dev/null || true
	$(MAKE) up-app

loadtest-app-up:  ## Point the ALREADY-RUNNING containerized api/agent-worker*/ingest-worker (`make up-app`) at loadtest/fake_llm_server.py instead of real litellm, via docker-compose.loadtest.yml's OPENAI_API_BASE-only override — every other container (Postgres, Redis, Grafana, cadvisor, ...) is untouched, so the Docker/cAdvisor dashboards keep reflecting the real load-testing containers too. Called by `loadtest-up` above, which also starts fake-llm itself — use this directly only if fake-llm is already running some other way.
	$(COMPOSE) -f deploy/compose/docker-compose.loadtest.yml up -d \
		api agent-worker agent-worker-support agent-worker-ops agent-worker-sales ingest-worker

loadtest-queued:  ## Interactive Locust UI against the queued path (loadtest/locustfile_queued.py, the only HTTP chat path this app serves) — needs `make loadtest-up` first (or the host-native equivalent from that file's own docstring); measures app/job_queue/agent_worker.py's own concurrency, not native Ollama's
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000

loadtest-queued-headless:  ## Fixed 20-user, 2-minute headless run of the queued-path scenario above → CSV + HTML report under loadtest/results-queued/
	mkdir -p loadtest/results-queued
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000 \
		--headless --users 20 --spawn-rate 5 --run-time 2m \
		--csv loadtest/results-queued/loadtest --html loadtest/results-queued/report.html

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

clear-checkpoints:  ## Truncate the LangGraph checkpointer's tables (checkpoints, checkpoint_blobs, checkpoint_writes): drops all saved conversation state, nothing else
	$(COMPOSE) exec postgres psql -U langfuse -d checkpointer -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"

clear-langfuse:  ## Truncate Langfuse's telemetry tables (traces, observations, scores, sessions, events, comments, media): keeps projects and API keys, so LANGFUSE_*_KEY still works
	$(COMPOSE) exec postgres psql -U langfuse -d langfuse -c "TRUNCATE traces, observations, scores, trace_sessions, events, comments, media, trace_media, observation_media CASCADE;"

clear-litellm:  ## Truncate LiteLLM's usage and spend logs (spend, error and audit logs, the Daily* tables): keeps users, teams, keys and model config, so the proxy and its UI keep working
	$(COMPOSE) exec postgres psql -U langfuse -d litellm -c "TRUNCATE \"LiteLLM_SpendLogs\", \"LiteLLM_SpendLogToolIndex\", \"LiteLLM_SpendLogGuardrailIndex\", \"LiteLLM_ErrorLogs\", \"LiteLLM_AuditLog\", \"LiteLLM_DailyUserSpend\", \"LiteLLM_DailyTeamSpend\", \"LiteLLM_DailyTagSpend\", \"LiteLLM_DailyEndUserSpend\", \"LiteLLM_DailyOrganizationSpend\", \"LiteLLM_DailyAgentSpend\", \"LiteLLM_DailyToolSpend\", \"LiteLLM_DailyGuardrailMetrics\", \"LiteLLM_DailyPolicyMetrics\" CASCADE;"

clear-all: clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm  ## Run every clear-* target (cache, Redis Streams, checkpointer, Langfuse, LiteLLM logs). Leaves appdata and volumes; `make clean` deletes volumes

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
