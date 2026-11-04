# Tests, static checks, load tests and the manual smoke checks. Included by the Makefile.

# --- Tests ---

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

# --- Static checks ---

lint:  ## Static checks: ruff (style/correctness) — see pyproject.toml's [tool.ruff]
	ruff check .

typecheck:  ## Static checks: mypy over app/ and scripts/ — see pyproject.toml's [tool.mypy]
	mypy

# --- Load tests ---

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

# --- Smoke and verification checks (manual) ---

authlimit-test:  ## Go vet + the authlimit service's own tests with the race detector (specs/018; no Docker, no services; needs Go)
	cd services/authlimit && go vet ./... && go test -race ./...

mcp-gateway-smoke: mcp-gateway-secrets  ## The OAuth + MCP end-to-end check against the running gateway stack, as a client does it (throwaway users, deleted after). Run it after `make mcp-gateway-ca` and `make mcp-gateway-bootstrap`, and after any change to the Casdoor digest. A manual tier, like test-sandbox: never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.mcp_gateway_smoke --ca $(GATEWAY_DIR)/caddy-root.crt

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
