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

# The default goal is named, not left to be the first target in the file: the includes below bring targets with them.
.DEFAULT_GOAL := help

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# Security scanners and the findings tracker (Semgrep, Trivy, Checkov, SonarQube, ZAP, Strix, DefectDojo).
include make/security.mk

# The application's own processes on this machine (serve, the workers, the channels, the MCP servers).
include make/host.mk

up:  ## Start all services (litellm, qdrant, langfuse, postgres, minio; talks to a native macOS Ollama on the host, see litellm-config.yaml)
	$(COMPOSE) up -d

up-app:  ## Start infra + the containerized app itself (api, agent-worker, ingest-worker; see Dockerfile)
	$(COMPOSE) --profile app up -d --build

# One compose project, every profile but `sandbox` (it needs OPENSANDBOX_API_KEY and mounts the Docker socket: `make sandbox-up`) and `quality` (SonarQube): infra, the
# app (api, every domain's agent worker, ingest worker) behind the front door in identity mode, the gateway profile (Casdoor, authlimit x2, Caddy), then the observability
# stack, which is its own compose project. The same overrides as authlimit-up-app, for the same reason: the compose file defaults to AUTH_MODE=headers and a Caddy upstream of the echo stub.
up-all: mcp-gateway-secrets authlimit-keys-ensure  ## Start EVERYTHING in containers: infra, the app behind the front door, the MCP gateway and observability (not the sandbox or SonarQube). Needs a native Ollama. Web UI: https://api.localhost:8443/ (in this mode the API refuses calls that skip the door). First time: `make mcp-gateway-bootstrap`, then `make mcp-gateway-ca`
	@curl -sf -o /dev/null http://localhost:11434/api/version || echo "warning: nothing answers on localhost:11434: start the native Ollama (ollama serve), the models are served from the host"
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build
	$(COMPOSE_OBS) up -d
	@echo "up. Web UI https://api.localhost:8443/ · Grafana http://localhost:3300 · Langfuse http://localhost:3000"
	@echo "first time only: make mcp-gateway-bootstrap, then make mcp-gateway-ca and trust the CA in your browser (README, Front door)"

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

probe-tools:  ## Does the configured chat model emit tool calls the app can use? 25 labelled prompts against the app's real tools plus 4 open-ended ones that must not loop; about 2 minutes on a native Ollama (`ARGS=--no-loops` is the seconds-long tool half). Run it after ANY change to a chat alias's model or repeat_penalty: make probe-tools [ARGS="--alias chat-strong"] [ARGS="--model qwen2.5:7b --repeat-penalty 1.3 1.1"] (GRAPH_PATTERNS.md pattern 73)
	python -m scripts.toolcall_probe $(ARGS)

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

mcp-gateway-up: mcp-gateway-secrets authlimit-keys-ensure  ## Start the MCP gateway stack (specs/017): Casdoor + its Postgres, the OAuth-protected gateway and a Caddy with local TLS on https://mcp.localhost:8443. Needs `make up` (the app database). Then `make mcp-gateway-bootstrap`
	$(GATEWAY_COMPOSE) up -d --build --no-deps casdoor-db casdoor mcp-gateway authlimit authlimit-echo caddy-gateway

mcp-gateway-bootstrap: mcp-gateway-secrets  ## Create (idempotently) the Casdoor organizations, applications, scopes and demo users the gateway expects, and print the `claude mcp add` commands. ARGS="--tenant acme --tenant other-co" adds tenants (default: ecorp)
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

authlimit-keys: authlimit-build  ## Manage the internal signing keys outside the repo. ARGS="init" (default, once) | "rotate --overlap 10m" | "list". Runs as you, so the files are yours (mode 600) and the container, which runs as you too, can read them
	@mkdir -p $(AUTHLIMIT_DIR) && chmod 700 $(AUTHLIMIT_DIR)
	@docker run --rm --user "$$(id -u):$$(id -g)" -v $(AUTHLIMIT_DIR):/work authlimit:dev keys $(or $(ARGS),init) --dir /work/keys

authlimit-keys-ensure:  # internal: create the first signing key if there is none (never replaces one)
	@test -f $(AUTHLIMIT_KEYS_DIR)/keyset.json || $(MAKE) --no-print-directory authlimit-keys

authlimit-up: mcp-gateway-up  ## The authlimit stack: same as mcp-gateway-up (the MCP routes always go through authlimit) - Casdoor, authlimit x2, the MCP gateway, a stub upstream and Caddy on https://{api,mcp,auth}.localhost:8443. Needs `make up` (Redis, Postgres); then `make mcp-gateway-bootstrap`

authlimit-up-app: mcp-gateway-secrets authlimit-keys-ensure  ## The authlimit stack with the REAL API behind the door instead of the echo stub (spec 018): the API in identity mode (it trusts only the signed identity the door adds), one agent worker, Caddy pointed at it. Needs `make up`; then `make mcp-gateway-bootstrap` and `make mcp-gateway-ca`. The web UI is at https://api.localhost:8443/
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

authlimit-revoke:  ## Void every token issued to a person, on every authlimit replica within seconds: make authlimit-revoke TENANT=ecorp SUB=<the person's id>   (`authlimit revoke --list` shows them)
	@test -n "$(TENANT)" -a -n "$(SUB)" || { echo "usage: make authlimit-revoke TENANT=<tenant> SUB=<subject>"; exit 2; }
	$(GATEWAY_COMPOSE) exec -T --index 1 authlimit /authlimit revoke --tenant "$(TENANT)" --sub "$(SUB)"

telegram-person:  ## Add, disable or re-enable a Telegram person at the authorization server (specs/020): make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>. Needs TELEGRAM_PERSON_SECRET in .env and the gateway stack's Casdoor
	@test -n "$(TELEGRAM_ID)" || { echo "usage: make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>"; exit 2; }
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.telegram_person $(or $(ACTION),add) --telegram-id "$(TELEGRAM_ID)"

ops-digest:  ## Run the ops bot's one-shot metrics digest, posting to the team channel (see scripts/ops_digest.py; meant for real cron)
	python -m scripts.ops_digest

followup-sweep:  ## Run the sales concierge's one-shot due-follow-up sweep, drafting nudges for a human to review (see scripts/followup_sweep.py; meant for real cron)
	python -m scripts.followup_sweep

tool-call-dedup-sweep:  ## Delete stale tool_call_dedup rows past their retention window (see scripts/tool_call_dedup_sweep.py; meant for real cron)
	python -m scripts.tool_call_dedup_sweep

budget-policy:  ## Operator CLI for per-tenant / per-person spend-limit overrides, e.g. `make budget-policy ARGS="show --tenant acme --principal alice"` (see scripts/budget_policy.py)
	python -m scripts.budget_policy $(ARGS)

litellm-key:  ## Mint / inspect the app's scoped, budget-capped LiteLLM key, set the default per-person budget, e.g. `LITELLM_MASTER_KEY=... make litellm-key ARGS="create --max-budget 600"` or `ARGS="person-budget set --max-budget 20"` (see scripts/litellm_key.py)
	python -m scripts.litellm_key $(ARGS)

gateway-check:  ## Run the PINNED LiteLLM image on the shipped dev config and a stub model backend: every alias priced, spend equals tokens x price, and nothing identifying (the readable end user, tenant, principal, conversation) reaches the backend. Needs Docker, no model, key or database. Re-run after bumping the image (infra/README.md)
	python -m scripts.gateway_check

web-search-check:  ## Does SERPER_API_KEY work? One real search through the web_search tool's own client; spends ONE serper.dev credit, prints the outcome and the first result, never the key. Exit 0 ok, 1 refused/failed, 2 no key (GRAPH_PATTERNS.md pattern 87)
	python -m scripts.web_search_check

usage-events-sweep:  ## Delete usage events older than USAGE_EVENT_RETENTION_DAYS (floor 35) (see scripts/usage_events_sweep.py; the table the caps read, so wire to cron only once your retention policy is decided)
	python -m scripts.usage_events_sweep

usage-events-carry-over:  ## One-time, idempotent: copy usage_ledger history older than the first usage event into usage_events, because the dollar caps sum the events and would otherwise forget the month so far. Run it before deploying that change and once after. ARGS=--dry-run counts and writes nothing; ARGS="--tenant acme" does one tenant (see scripts/usage_events_carry_over.py)
	python -m scripts.usage_events_carry_over $(ARGS)

billing-usage-sweep:  ## Delete the billing package's usage events older than BILLING_USAGE_EVENT_RETENTION_DAYS (floor 35), never one whose export never finished (credit-billing-usage-sweep; wire to cron once your retention policy is decided)
	python -m credit_billing.cli.usage_sweep

billing-migrate:  ## Apply the billing package's schema scripts to an existing database, in order, and record them (see packages/billing/src/credit_billing/cli/migrate.py; safe to re-run and to run twice at once; `ARGS=--lock-timeout-seconds=30` to wait longer)
	python -m credit_billing.cli.migrate $(ARGS)

billing-migrate-status:  ## Show which billing schema scripts are applied, pending or changed, and write nothing; `make billing-migrate-status ARGS=--check` exits 1 if any is not current (a deploy gate)
	python -m credit_billing.cli.migrate --status $(ARGS)

billing-inbox-sweep:  ## Delete applied/ignored billing webhook inbox rows older than BILLING_INBOX_RETENTION_DAYS (see packages/billing/src/credit_billing/cli/inbox_sweep.py; never touches open or quarantined rows; wire to cron once your retention policy is decided)
	python -m credit_billing.cli.inbox_sweep

credits:  ## Operator CLI for the credit wallet, e.g. `make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up'"`, `adjust` (signed) and `show` (balance, lots, entries); --by and --reason are required for a change (see packages/billing/src/credit_billing/cli/credits.py)
	python -m credit_billing.cli.credits $(ARGS)

credit-reconcile:  ## Wallet check: was every recorded usage worth credits actually charged? Over the last CREDIT_RECONCILE_LOOKBACK_DAYS; exit 0 charged / 1 drift / 2 could not finish / 3 database unreachable (the billing package's credit-billing-reconcile; ARGS="--days 7 --json")
	python -m credit_billing.cli.reconcile $(ARGS)

usage-reconcile:  ## Meter check: do the usage events agree with the LLM gateway's spend log over the last USAGE_RECONCILE_LOOKBACK_DAYS? exit 0 agree / 1 drift / 2 could not finish. Needs LITELLM_MASTER_KEY; ARGS="--days 7 --json" (see scripts/usage_reconcile.py)
	python -m scripts.usage_reconcile $(ARGS)

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

eval:  ## Run the golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.eval

model-eval:  ## Evaluate ONE model against its recorded baseline: make model-eval KIND=embedding|reranker|screen|classifier CANDIDATE=<name> [VIA=gateway|service|inprocess] [DATASET=<name>] [OUTCOMES=<file>] (a label set: KIND=classifier CANDIDATE=labelset-bge-small-en-v1.5 DATASET=route-topics). A manual tier like `make eval` (needs models/services); specs/014, evals/README.md
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval KIND=embedding CANDIDATE=<name> [VIA=...] [DATASET=...]"; exit 2; }
	python -m scripts.model_eval run --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(OUTCOMES),--outcomes $(OUTCOMES))

model-eval-all:  ## The same candidate for every kind that declares it: make model-eval-all CANDIDATE=<name> (a control such as baseline-as-candidate runs for all)
	@test -n "$(CANDIDATE)" || { echo "usage: make model-eval-all CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval run --all-kinds --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA))

model-eval-baseline:  ## RECORD a baseline (immutable; FORCE=1 to replace, ADD_HARDWARE=1 to add this machine's latency): make model-eval-baseline KIND=... CANDIDATE=...
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval-baseline KIND=embedding CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval baseline --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(FORCE),--force) $(if $(ADD_HARDWARE),--add-hardware)

model-eval-data:  ## Download and verify the pinned public evaluation datasets (needs the network once; offline afterwards)
	python -m scripts.model_eval fetch

model-eval-verify:  ## Check every committed evaluation dataset, baseline, licence and control, with no model and no network
	python -m scripts.model_eval verify

labelset-calibrate:  ## Choose a label set's "none of these" cutoff for ONE model on the dev split (needs the model cached): make labelset-calibrate CANDIDATE=labelset-bge-small-en-v1.5 [LABELSET=route-topics] [KEEP=0.95]. Prints the line to put under `calibration` in docker/ml-service/labelsets/<set>.json; specs/015
	@test -n "$(CANDIDATE)" || { echo "usage: make labelset-calibrate CANDIDATE=<a classifier candidate with source.model, e.g. labelset-bge-small-en-v1.5>"; exit 2; }
	python -m scripts.model_eval labelset-calibrate --candidate $(CANDIDATE) $(if $(LABELSET),--labelset $(LABELSET)) $(if $(KEEP),--keep $(KEEP))

router-label:  ## Score the route prompts' answers from the local and strong model with a judge (minutes per prompt; needs the gateway): make router-label JUDGE=<alias> [LOCAL=chat] [STRONG=chat-strong] [DEADLINE=<seconds>] [LIMIT=<n>]. Writes the ignored outcomes/ file; resumable
	@test -n "$(JUDGE)" || { echo "usage: make router-label JUDGE=<gateway alias that scores the answers>"; exit 2; }
	python -m scripts.model_eval route-label --judge $(JUDGE) $(if $(LOCAL),--local $(LOCAL)) $(if $(STRONG),--strong $(STRONG)) $(if $(STRONG_ID),--strong-id $(STRONG_ID)) $(if $(DEADLINE),--deadline $(DEADLINE)) $(if $(LIMIT),--limit $(LIMIT))

promptfoo:  ## Prompt-level regression checks for the domain system prompts against a real Ollama (needs `make up` or a native Ollama with CHAT_MODEL pulled)
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	cd promptfoo && for domain in support ops sales; do \
		npx promptfoo eval --config $$domain.yaml || exit 1; \
	done

promptfoo-redteam:  ## Adversarial variants of the support/sales/ops prompts (prompt injection, policy violations, sandbox misuse) against the real local target, generated+graded by Gemini 3.1 Flash-Lite (needs GOOGLE_API_KEY, see .env.example) — see promptfoo/redteam.yaml's own comments for why redteam.provider is the one deliberate exception to this project's offline commitment
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	# `--max-concurrency 1 --delay 2100`: keeps every domain's redteam.provider
	# calls serial with >2s between them, safely under the free Gemini tier's
	# 30 req/min this was built against (see redteam.yaml's own RATE LIMIT
	# paragraph) — raise only alongside a paid tier/higher quota.
	# Every promptfoo command runs from inside promptfoo/, where package.json and node_modules live
	# (so the repo root stays Python-only); --config and --output are therefore relative to that directory.
	# `redteam run`'s generated+graded test suite is written to `-o`/
	# `--output`, which DEFAULTS to a HARDCODED `redteam.yaml` in the
	# --config file's own DIRECTORY — not to the --config file itself,
	# regardless of what it's named (confirmed directly in promptfoo's own
	# installed source, promptfoo/node_modules/promptfoo/dist/src/main.js:
	# `redteamPath = path.join(configDir, "redteam.yaml")`). Copying each
	# domain's config to its own scratch file first does NOT protect the
	# hand-authored sources on its own — every scratch file already lives in
	# THIS SAME `promptfoo/` directory, so all three would silently default
	# to overwriting `promptfoo/redteam.yaml` (support's own hand-authored
	# file) regardless of which domain actually ran. An explicit `-o`
	# pointed back at each stanza's own scratch file is what actually keeps
	# them isolated — verified the hard way: an early version of this target
	# without it clobbered the checked-in `redteam.yaml` from a run against
	# an unrelated scratch config. Three explicit stanzas, not a
	# `for domain in ...` loop like the `promptfoo` target above — support's
	# file is `redteam.yaml` (no domain prefix, predates the sales/ops
	# siblings), so the source filename isn't uniform across domains the way
	# `promptfoo/$$domain.yaml` already is.
	#
	# Leading `-` on each `redteam run` line: promptfoo's OWN exit code is
	# nonzero (a hardcoded 100 by default) the moment ANY test case fails —
	# confirmed directly in its installed source (`process.exitCode =
	# failedTestExitCode ?? 100`), verified the hard way when a real Gemini
	# run found a genuine finding in `sales-redteam` and `make` aborted
	# right there, never reaching `ops-redteam` at all. That's the opposite
	# of what a MANUAL, read-it-by-hand target wants — a finding here isn't
	# a bug in this target, it's the entire point of running it — so `-`
	# tells `make` to keep going to the next domain regardless.
	-cp promptfoo/redteam.yaml promptfoo/.support-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .support-redteam-run-scratch.yaml --output .support-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/sales-redteam.yaml promptfoo/.sales-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .sales-redteam-run-scratch.yaml --output .sales-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/ops-redteam.yaml promptfoo/.ops-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .ops-redteam-run-scratch.yaml --output .ops-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100

deepeval:  ## LLM-judged RAG quality (tests/deepeval/test_rag_quality_deepeval.py) + a multi-turn conversation simulation (test_conversation_simulator_deepeval.py) + tool-call trajectory correctness (test_tool_correctness_deepeval.py) against the real graph — needs Docker + GOOGLE_API_KEY (RAG quality/tool correctness, tests/deepeval/conftest.py's deepeval_judge fixture) + GROQ_API_KEY (conversation simulation only — Gemini's response_schema can't grade its KnowledgeRetentionMetric, see deepeval_conversation_judge); read the printed reasons by hand, don't trust pass/fail alone (see those files' own disclosed judge-reliability findings, GRAPH_PATTERNS.md pattern 48). Same command CI's own `deepeval` job runs; every file's test cases are `flaky=True` there so a bad score can't fail the build.
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s --reruns 2 --reruns-delay 10 --only-rerun "(ServerError|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|RateLimitError|APITimeoutError|APIConnectionError|InternalServerError)" tests/deepeval

garak:  ## Fast, curated probe subset scanning the real model for known jailbreak/injection patterns — needs `make up`/a native Ollama AND a SEPARATE Python environment, never this repo's own .venv (installing garak here upgrades langgraph-checkpoint past what this app's own pin allows — see garak/requirements-garak.txt)
	python -m pip install -q -r garak/requirements-garak.txt
	python garak/run_ci_scan.py

garak-full:  ## The full, slow garak probe suite — deliberate, pre-release scanning, not a per-PR gate (same framing as `make eval`); same separate-environment requirement as `make garak`
	python -m pip install -q -r garak/requirements-garak.txt
	OPENAICOMPATIBLE_API_KEY="sk-not-checked-by-ollama" python -m garak --config garak/config.yaml --target_type openai.OpenAICompatible --target_name "$${GARAK_MODEL:-qwen2.5:3b}"

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

# `down` leaves every container a profile started (app, gateway, sandbox, quality) running, and then cannot remove the network; `--profile '*'` names them all.
down-all: mcp-gateway-secrets  ## Stop and remove EVERY container of the dev stack, whatever profile started it (infra, app, gateway, sandbox, SonarQube), and the observability stack. Keeps volumes (`make clean` deletes them). Host processes (`make serve`, `agent-worker`, `telegram`) are not containers and keep running
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
