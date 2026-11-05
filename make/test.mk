# Tests, static checks, load tests and the manual smoke checks. Included by the Makefile.

# --- Tests ---

test:  ## Graph test suite in parallel (hermetic: fake LLM, no Qdrant), then the billing package's tests in a separate process (both have a `tests` package)
	pytest -n auto -q
	cd packages/billing && pytest -q

test-billing-package:  ## The billing package's hermetic tests alone, from its directory (the agent app is not on the path; spec 011)
	cd packages/billing && pytest -q

test-integration:  ## Real Postgres/Redis/Qdrant/ml-service/crawl4ai via testcontainers (no LLM); needs Docker, not `make up` (GRAPH_PATTERNS.md 48/50)
	# --dist=loadgroup: test_worker_scaling.py's xdist_group marker is ignored by plain `load`.
	pytest -n auto -m integration -q --dist=loadgroup
	cd packages/billing && pytest -m integration -q

test-live:  ## Real small Ollama + ml-service + full app/worker stack via testcontainers, incl. Playwright E2E and crawl4ai renders; needs Docker (patterns 48/50)
	playwright install --with-deps chromium
	pytest -n auto -m "llm or e2e or crawl" -q

test-sandbox:  ## Real OpenSandbox MCP round trip (pattern 50); needs `make sandbox-up` and opensandbox-mcp on PATH, else skips. Manual, never CI
	pytest -m sandbox -q -s

.PHONY: test-provider-sandbox
test-provider-sandbox:  ## A payment provider's real SANDBOX (specs/010 T029): Checkout requests and signed webhooks via `stripe listen` / `polar listen`. Needs STRIPE_API_KEY (test mode) and/or POLAR_ACCESS_TOKEN (sandbox) in .env plus the provider CLI; skips without them, refuses a live key, creates labelled sandbox objects. Manual, never CI
	pytest -c packages/billing/pyproject.toml packages/billing/tests -m provider_sandbox -q -s -p no:xdist

# --- Static checks ---

lint:  ## ruff over the repo (config in pyproject.toml)
	ruff check .

typecheck:  ## mypy over app/ and scripts/ (config in pyproject.toml)
	mypy

# --- Load tests ---

loadtest-up:  ## Before `make loadtest-queued`: (re)start loadtest/fake_llm_server.py as a background host process (var/fake-llm.log) and point the containerized api and workers at it. Safe to re-run; counterpart `make loadtest-down`
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

loadtest-down:  ## Stop the fake LLM and point the api and workers back at real litellm (`make up-app`)
	pkill -f 'loadtest.fake_llm_server' 2>/dev/null || true
	$(MAKE) up-app

loadtest-app-up:  ## Point the running containerized api and workers at loadtest/fake_llm_server.py via docker-compose.loadtest.yml (only OPENAI_API_BASE changes; other containers untouched). Called by `loadtest-up`; use it alone only if the fake LLM already runs
	$(COMPOSE) -f deploy/compose/docker-compose.loadtest.yml up -d \
		api agent-worker ingest-worker

loadtest-queued:  ## Interactive Locust UI against the queued chat path (loadtest/locustfile_queued.py); needs `make loadtest-up`. Measures agent_worker's own concurrency, not Ollama's
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000

loadtest-queued-headless:  ## Headless 20-user, 2-minute run of the same scenario; CSV and HTML report in loadtest/results-queued/
	mkdir -p loadtest/results-queued
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000 \
		--headless --users 20 --spawn-rate 5 --run-time 2m \
		--csv loadtest/results-queued/loadtest --html loadtest/results-queued/report.html

# --- Smoke and verification checks (manual) ---

authlimit-test:  ## go vet and the authlimit service's tests with the race detector (specs/018; needs Go, no Docker)
	cd services/authlimit && go vet ./... && go test -race ./...

mcp-gateway-smoke: mcp-gateway-secrets  ## OAuth + MCP end-to-end check against the running gateway stack, as a client does it (throwaway users, deleted after). Run after `make mcp-gateway-ca` and `make mcp-gateway-bootstrap`, and after any change to the Casdoor digest. Manual, never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.mcp_gateway_smoke --ca $(GATEWAY_DIR)/caddy-root.crt

authlimit-smoke: mcp-gateway-secrets  ## End-to-end check of the front door: sign-in, refusal, rate limit, revocation, replica kill, Casdoor outage, key rotation (throwaway users). Run after `make authlimit-up`, `make mcp-gateway-ca` and `make mcp-gateway-bootstrap`. Manual, never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_ENV) python -m scripts.checks.authlimit_smoke --ca $(GATEWAY_DIR)/caddy-root.crt --keys-dir $(AUTHLIMIT_KEYS_DIR) --compose '$(GATEWAY_COMPOSE_CMD)' $(ARGS)

authlimit-web-check:  ## The web page in a REAL browser (Playwright): sign in through Casdoor, chat, see only your tenant's rows, sign out, refused token (specs/018). Run after `make authlimit-up-app`, `make mcp-gateway-bootstrap ARGS="--tenant ecorp --tenant other-co"` and `make mcp-gateway-ca`, with the worker on loadtest/fake_llm_server.py. Manual, never CI
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.authlimit_web_check --ca $(GATEWAY_DIR)/caddy-root.crt $(ARGS)

authlimit-latency:  ## Time the front door's check adds to a request: a checked route against a public one, alternating (specs/018 SC-008). Run after `make authlimit-up` and `make mcp-gateway-bootstrap`; ARGS="-n 1000 --path /chat/sessions"
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.authlimit_latency --ca $(GATEWAY_DIR)/caddy-root.crt $(ARGS)

authlimit-mutants:  ## Apply each deliberate defect of the authlimit mutation list and require a test to fail (specs/018 SC-010). Edits the source in place and restores it: run on a clean tree; needs Go
	python scripts/checks/authlimit_mutants.py

gateway-check:  ## Run the PINNED LiteLLM image on the shipped dev config and a stub backend: every alias priced, spend = tokens x price, nothing identifying reaches the backend. Needs Docker only; re-run after bumping the image (deploy/README.md)
	python -m scripts.checks.gateway_check

web-search-check:  ## Does SERPER_API_KEY work? One real search; spends ONE serper.dev credit, prints the outcome and first result, never the key. Exit 0 ok / 1 failed / 2 no key (GRAPH_PATTERNS.md 87)
	python -m scripts.checks.web_search_check
