.PHONY: help up up-app sandbox-up sandbox-build pull-models ingest index-skills chat chat-hitl serve mcp-serve mcp-serve-ops telegram telegram-support telegram-sales agent-worker agent-worker-support agent-worker-ops agent-worker-sales restart-all fake-llm ingest-worker ops-digest followup-sweep test test-integration test-live test-sandbox lint typecheck eval promptfoo promptfoo-redteam deepeval garak garak-full trivy trivy-image semgrep checkov sonar-up sonar-down sonar-scan zap-baseline zap-api-scan zap-view defectdojo-up defectdojo-down defectdojo-import loadtest-queued loadtest-queued-headless strix strix-app strix-view logs down clean clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm clear-all obs-up obs-down obs-logs obs-clean

# Pinned DefectDojo release — see `defectdojo-up`'s own comment for why this
# is a plain git clone into ~/.cache (NOT vendored into this repo, same
# "external tool, own lifecycle" treatment `make strix` already gives
# pipx-installed strix-agent) rather than a docker-compose.yml service like
# sonarqube/sonarqube-db above: DefectDojo is a 6-container app with its own
# release cadence, not a two-container official image this repo can just
# declare inline. DJANGO_VERSION/NGINX_VERSION below are exported to match —
# verified directly both `defectdojo/defectdojo-django:3.3.100` and
# `defectdojo/defectdojo-nginx:3.3.100` exist on Docker Hub, not assumed
# from the compose file's own `:latest` default.
DEFECTDOJO_VERSION := 3.3.100
DEFECTDOJO_DIR := $(HOME)/.cache/agent-core-demo-defectdojo

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start all services (ollama, litellm, qdrant, langfuse, postgres, minio)
	docker compose up -d

up-app:  ## Start infra + the containerized app itself (api, agent-worker, ingest-worker; see Dockerfile)
	docker compose --profile app up -d --build

sandbox-up: sandbox-build  ## Start the containerized, authenticated OpenSandbox server (docker/opensandbox-server.{Dockerfile,toml}) that the ops/support/sales domains' sandbox tools execute against — opt-in `sandbox` profile like `up-app`'s `app` profile, not part of plain `make up`, since it bind-mounts the host Docker socket to create sibling sandbox containers. Needs OPENSANDBOX_API_KEY set in .env first (see .env.example) — the server refuses to start without one.
	docker compose --profile sandbox up -d --build

sandbox-build:  ## Build the shared sandbox base image (docker/sandbox.Dockerfile — python:3.12-slim + numpy + pandas, non-root, no network egress) that SANDBOX_IMAGE (app/core/config.py) references. Shared by every domain's sandbox tools, not ops-specific. A plain `docker build`, not a docker-compose service — opensandbox-server pulls it by tag from the same host Docker daemon it already has via its bind-mounted socket. Run again after editing that Dockerfile.
	docker build -t agent-core-demo-sandbox:latest -f docker/sandbox.Dockerfile .

pull-models:  ## Download the Ollama chat + embedding models
	docker compose exec ollama ollama pull qwen2.5:3b
	docker compose exec ollama ollama pull nomic-embed-text

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m scripts.seed

index-skills:  ## Embed the skills/ catalog (name+description) and upsert into its own Qdrant collection
	python -m scripts.index_skills

chat:  ## Start the interactive LangGraph agent CLI (astream_events v2, shows tool calls)
	python -m app.channels.chat

chat-hitl:  ## Interactive CLI with human-in-the-loop tool approval
	python -m app.channels.chat --hitl

serve:  ## Start the FastAPI service (http://localhost:8000/docs)
	# --reload-dir scopes the file watcher to app/ only. Without it, uvicorn
	# watches the ENTIRE working directory recursively, including .venv —
	# 17k+ files here vs ~150 in app/ — and continuously re-scans that whole
	# tree (verified empirically via `sample`: hundreds of lstat/open/
	# getdirentries syscalls per second). That's real, sustained CPU
	# competing directly with Ollama's own CPU-bound inference in this
	# docker-compose stack — on a resource-constrained machine it was
	# measurably slowing down/timing out ordinary chat turns.
	uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app

mcp-serve:  ## Start the MCP server exposing query_employees (stdio transport; needs `make up`)
	python -m app.mcp.server

mcp-inspect:  ## Launch the MCP Inspector against app/mcp/server.py for interactive testing
	mcp dev app/mcp/server.py

mcp-serve-ops:  ## Start the MCP server exposing fetch_metrics_summary/list_recent_incidents (stdio transport; needs `make up`) — see app/mcp/ops_server.py
	python -m app.mcp.ops_server

telegram:  ## Start the Telegram bot channel for the Ecorp domain (needs TELEGRAM_BOT_TOKEN in .env; see app/channels/telegram.py)
	python -m app.channels.telegram

telegram-support:  ## Start the Telegram channel as the Tier-1 support copilot (see app/domains/support/)
	AGENT_DOMAIN=support python -m app.channels.telegram

telegram-sales:  ## Start the Telegram channel as the sales/CRM concierge (see app/domains/sales/)
	AGENT_DOMAIN=sales python -m app.channels.telegram

agent-worker:  ## Start a Redis Streams agent worker for the Ecorp domain (run several for independent scaling; see POST /chat/stream/queued)
	python -m app.turns.agent_worker

agent-worker-support:  ## Start an agent worker pool for the Tier-1 support domain (see app/domains/support/); the web UI's X-Domain: support turns route here
	AGENT_DOMAIN=support python -m app.turns.agent_worker

agent-worker-ops:  ## Start an agent worker pool for the ops domain (see app/domains/ops/); the web UI's X-Domain: ops turns route here
	AGENT_DOMAIN=ops python -m app.turns.agent_worker

agent-worker-sales:  ## Start an agent worker pool for the sales/CRM domain (see app/domains/sales/); the web UI's X-Domain: sales turns route here
	AGENT_DOMAIN=sales python -m app.turns.agent_worker

restart-all:  ## Kill and relaunch the API service (which also serves the built-in web UI) + every domain's agent-worker pool + the ingest-worker as backgrounded host processes (logs under var/*.log), then reset+re-seed ingest data (`make ingest`) and the skills search index (`make index-skills`) — host-native dev convenience; not for the containerized `up-app` stack
	pkill -f 'uvicorn app\.api\.main:app' 2>/dev/null || true
	# Reload mode (`serve`'s own `--reload`) runs the real server as a
	# `multiprocessing` worker whose OS-level command line is just a generic
	# Python multiprocessing bootstrap string — verified directly this
	# means the pkill above can NEVER match it, only the reloader parent.
	# If that parent has already died for any reason (crashed, terminal
	# closed), the worker is silently orphaned: still bound to :8000, still
	# serving stale code, forever invisible to the pkill above. Killing
	# whatever actually holds the port catches that case.
	lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
	pkill -f 'app\.turns\.agent_worker' 2>/dev/null || true
	pkill -f 'app\.ingestion\.ingest_worker' 2>/dev/null || true
	# A killed process doesn't free its port / stop matching `pgrep`
	# instantly — verified directly this isn't theoretical: a plain
	# `sleep 1` here twice left a stale agent-worker alive long enough to
	# start a SECOND, duplicate worker for the same domain (both then
	# competing for the same Redis consumer group), and separately caused
	# a real code/config change to silently not take effect because the
	# stale process was still the one actually handling requests. Poll
	# for actual exit instead of guessing a fixed delay; SIGKILL anything
	# still alive after 10s rather than waiting forever.
	for i in $$(seq 1 20); do \
		lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 || pgrep -f 'app\.turns\.agent_worker' >/dev/null 2>&1 || pgrep -f 'app\.ingestion\.ingest_worker' >/dev/null 2>&1 || break; \
		sleep 0.5; \
	done
	lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	pkill -9 -f 'app\.turns\.agent_worker' 2>/dev/null || true
	pkill -9 -f 'app\.ingestion\.ingest_worker' 2>/dev/null || true
	mkdir -p var
	nohup $(MAKE) serve > var/serve.log 2>&1 &
	nohup $(MAKE) agent-worker > var/agent-worker.log 2>&1 &
	nohup $(MAKE) agent-worker-support > var/agent-worker-support.log 2>&1 &
	nohup $(MAKE) agent-worker-ops > var/agent-worker-ops.log 2>&1 &
	nohup $(MAKE) agent-worker-sales > var/agent-worker-sales.log 2>&1 &
	nohup $(MAKE) ingest-worker > var/ingest-worker.log 2>&1 &
	$(MAKE) ingest
	$(MAKE) index-skills

fake-llm:  ## Start the fake concurrent-LLM double (loadtest/fake_llm_server.py, :9009) for load-testing agent_worker.py's own concurrency in isolation from native Ollama's hard `-np 1` ceiling — see that file's docstring for how to point a run at it
	uvicorn loadtest.fake_llm_server:app --host 0.0.0.0 --port 9009

ingest-worker:  ## Start a document-ingestion worker (PDF/DOCX uploads; run several for independent scaling; see POST /ingest/upload)
	python -m app.ingestion.ingest_worker

ops-digest:  ## Run the ops bot's one-shot metrics digest, posting to the team channel (see scripts/ops_digest.py; meant for real cron)
	python -m scripts.ops_digest

followup-sweep:  ## Run the sales concierge's one-shot due-follow-up sweep, drafting nudges for a human to review (see scripts/followup_sweep.py; meant for real cron)
	python -m scripts.followup_sweep

test:  ## Run the graph test suite in parallel (no live services needed — fake LLM, no Qdrant)
	pytest -n auto -q

test-integration:  ## Real Postgres/Redis/Qdrant/ml-service/crawl4ai via testcontainers (no LLM) — needs Docker, no `make up` required (GRAPH_PATTERNS.md pattern 48/50)
	# --dist=loadgroup: tests/integration/test_worker_scaling.py's own
	# xdist_group marker needs this to actually take effect (plain `load`
	# ignores it) — see that module's own comment for why.
	pytest -n auto -m integration -q --dist=loadgroup

test-live:  ## Real small Ollama model + ml-service + full app/agent-worker stack via testcontainers, incl. Playwright browser E2E and real crawl4ai renders — needs Docker (pattern 48/50)
	playwright install --with-deps chromium
	pytest -n auto -m "llm or e2e or crawl" -q

test-sandbox:  ## Real OpenSandbox MCP round trip (pattern 50) — needs `make sandbox-up` running separately AND opensandbox-mcp on PATH; self-skips cleanly if either isn't there. Deliberately manual, like `make deepeval`/`garak` — never CI
	pytest -m sandbox -q -s

lint:  ## Static checks: ruff (style/correctness) — see pyproject.toml's [tool.ruff]
	ruff check .

typecheck:  ## Static checks: mypy over app/ and scripts/ — see pyproject.toml's [tool.mypy]
	mypy

eval:  ## Run the golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.eval

promptfoo:  ## Prompt-level regression checks for the domain system prompts against a real Ollama (needs `make up` or a native Ollama with CHAT_MODEL pulled)
	npm install --include=optional
	python -m promptfoo.dump_prompts
	for domain in support ops sales; do \
		npx promptfoo eval --config promptfoo/$$domain.yaml || exit 1; \
	done

promptfoo-redteam:  ## Adversarial variants of the support/sales/ops prompts (prompt injection, policy violations, sandbox misuse) against the real local target, generated+graded by Gemini 3.1 Flash-Lite (needs GOOGLE_API_KEY, see .env.example) — see promptfoo/redteam.yaml's own comments for why redteam.provider is the one deliberate exception to this project's offline commitment
	npm install --include=optional
	python -m promptfoo.dump_prompts
	# `--max-concurrency 1 --delay 2100`: keeps every domain's redteam.provider
	# calls serial with >2s between them, safely under the free Gemini tier's
	# 30 req/min this was built against (see redteam.yaml's own RATE LIMIT
	# paragraph) — raise only alongside a paid tier/higher quota.
	# `redteam run`'s generated+graded test suite is written to `-o`/
	# `--output`, which DEFAULTS to a HARDCODED `redteam.yaml` in the
	# --config file's own DIRECTORY — not to the --config file itself,
	# regardless of what it's named (confirmed directly in promptfoo's own
	# installed source, node_modules/promptfoo/dist/src/main.js:
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
	-PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config promptfoo/.support-redteam-run-scratch.yaml --output promptfoo/.support-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/sales-redteam.yaml promptfoo/.sales-redteam-run-scratch.yaml
	-PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config promptfoo/.sales-redteam-run-scratch.yaml --output promptfoo/.sales-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/ops-redteam.yaml promptfoo/.ops-redteam-run-scratch.yaml
	-PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config promptfoo/.ops-redteam-run-scratch.yaml --output promptfoo/.ops-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100

deepeval:  ## LLM-judged RAG quality (tests/deepeval/test_rag_quality_deepeval.py) + a multi-turn conversation simulation (test_conversation_simulator_deepeval.py) + tool-call trajectory correctness (test_tool_correctness_deepeval.py) against the real graph — needs Docker + GOOGLE_API_KEY (see .env.example, tests/deepeval/conftest.py's deepeval_judge fixture); read the printed reasons by hand, don't trust pass/fail alone (see those files' own disclosed judge-reliability findings, GRAPH_PATTERNS.md pattern 48). Same command CI's own `deepeval` job runs; every file's test cases are `flaky=True` there so a bad score can't fail the build.
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s tests/deepeval

garak:  ## Fast, curated probe subset scanning the real model for known jailbreak/injection patterns — needs `make up`/a native Ollama AND a SEPARATE Python environment, never this repo's own .venv (installing garak here upgrades langgraph-checkpoint past what this app's own pin allows — see garak/requirements-garak.txt)
	python -m pip install -q -r garak/requirements-garak.txt
	python garak/run_ci_scan.py

garak-full:  ## The full, slow garak probe suite — deliberate, pre-release scanning, not a per-PR gate (same framing as `make eval`); same separate-environment requirement as `make garak`
	python -m pip install -q -r garak/requirements-garak.txt
	OPENAICOMPATIBLE_API_KEY="sk-not-checked-by-ollama" python -m garak --config garak/config.yaml --target_type openai.OpenAICompatible --target_name "$${GARAK_MODEL:-qwen2.5:3b}"

trivy:  ## Scan dependencies/Dockerfile+compose/secrets for known vulns (aquasec/trivy via Docker — no local trivy install needed; same policy as CI's `trivy` job)
	# --file-patterns points trivy's pip analyzer at requirements-lock.txt,
	# not requirements.txt (trivy's own default match) — verified directly
	# this matters: requirements.txt's version RANGES (`langgraph>=0.2.20,<0.3`)
	# don't resolve to one installed version to check against the CVE
	# database, so scanning it alone silently finds nothing. Scanning the
	# exact-pinned lock file — what the Dockerfile/CI actually install,
	# per requirements-lock.txt's own header — surfaced 5 real HIGH-severity
	# fixable CVEs on first run here, including an RCE in langgraph-checkpoint
	# (CVE-2025-64439); see this repo's own disclosed pin-compatibility
	# constraints (garak/requirements-garak.txt) before bumping it.
	# infra/terraform excluded — Checkov (`make checkov`) owns that
	# directory's IaC scanning; see CI's `trivy` job for why (Trivy's own
	# DigitalOcean firewall policies flag this repo's necessary public
	# 80/443 ingress + broad egress as CRITICAL with no way to distinguish
	# it from an actually-dangerous open port).
	docker run --rm -v $(PWD):/repo aquasec/trivy:0.74.0 fs \
		--scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed \
		--file-patterns 'pip:requirements-lock\.txt$$' \
		--skip-dirs .venv,node_modules,.git,.mypy_cache,.ruff_cache,.pytest_cache,infra/terraform /repo

trivy-image:  ## Build the app image (see Dockerfile) and scan it for OS/library vulnerabilities
	docker build -t agent-core-demo:trivy .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 image \
		--severity HIGH,CRITICAL --ignore-unfixed agent-core-demo:trivy

semgrep:  ## Static analysis (SAST) over app/scripts/docker/Dockerfile via Docker (no local install) — same rulesets and hard gate as CI's `semgrep` job; see that job's own comment for why these `p/*` configs need no `semgrep login`
	docker run --rm -v $(PWD):/src -w /src semgrep/semgrep:1.177.0 semgrep scan --error --metrics=off \
		--config p/security-audit --config p/secrets --config p/python --config p/dockerfile \
		app/ scripts/ docker/ Dockerfile

checkov:  ## Scan infra/terraform for IaC misconfigurations — same config/gate as CI's `checkov` job. pip install, not Docker: verified directly bridgecrewio/checkov's Docker Hub image no longer pulls ("repository does not exist"), unlike every other Docker-based scanner target in this file
	pip install -q checkov==3.3.17
	checkov --config-file .checkov.yaml

sonar-up:  ## Start a PERSISTENT self-hosted SonarQube (Community Edition) at http://localhost:9002 — opt-in `quality` profile (see docker-compose.yml's own comment on why 9002, not 9000/9001, and why this is separate from CI's ephemeral `sonarqube` job). First login: admin/admin, then change the password before running `make sonar-scan` for real (a throwaway local instance can skip that, same as CI's own ephemeral one).
	docker compose --profile quality up -d sonarqube-db sonarqube

sonar-down:  ## Stop the persistent SonarQube server (keeps its volumes — history/trends survive)
	docker compose --profile quality stop sonarqube sonarqube-db

sonar-scan:  ## Run a scan against the persistent `make sonar-up` server (needs SONAR_TOKEN — generate one under My Account -> Security in the UI first). The very first baseline scan of this repo found ~90 pre-existing issues (2 BLOCKER, 10 CRITICAL) that the default "clean new code" quality gate does NOT block on — see the dashboard to triage them; that gate only fails a scan when NEW code introduces a new issue.
	@test -n "$$SONAR_TOKEN" || (echo "SONAR_TOKEN is required — generate one in the SonarQube UI (My Account -> Security)" && exit 1)
	# Joins docker-compose.yml's own network and talks to the `sonarqube`
	# service by its in-network name/port (9000, not the 9002 host
	# publish) — NOT `--network host` (what CI's ephemeral job uses, see
	# that job's own comment): host networking is Linux-only-reliable and
	# this target needs to work the same on a Mac dev machine too.
	docker run --rm --network agent-core-demo_default \
		-e SONAR_HOST_URL=http://sonarqube:9000 -e SONAR_TOKEN=$$SONAR_TOKEN \
		-v $(PWD):/usr/src -w /usr/src \
		sonarsource/sonar-scanner-cli@sha256:23ca0f137965d9dff2198074043fd48d386280bc5d0ccac8c8349cea4cf096a9

zap-baseline:  ## OWASP ZAP baseline DAST scan (spider + PASSIVE rules only, no active attacks — safe against anything you merely have read access to) against a running target, default the local API (`make serve`/`make up-app`). Override with ZAP_TARGET=<url>. Docker-based (zaproxy/zap-stable, no local ZAP install needed); reports land under zap_reports/ (gitignored) as HTML + the XML format `make defectdojo-import` (DefectDojo's own ZAP parser — verified directly it wants XML, not the JSON zap-baseline.py can also emit) expects.
	mkdir -p zap_reports
	# --add-host: this is a standalone `docker run`, not joined to
	# docker-compose.yml's own network, so it needs the same
	# host.docker.internal mapping that file's own `api`/`agent-worker`
	# services set up for themselves (extra_hosts) to reach a host-process
	# `make serve` OR the containerized `make up-app` (both publish :8000
	# to the host either way) — native Linux Docker doesn't wire this up
	# for a bare `docker run` the way Docker Desktop does automatically.
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-baseline.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000} \
		-x baseline-report.xml -r baseline-report.html -I

zap-api-scan:  ## OWASP ZAP ACTIVE scan driven by the app's own OpenAPI spec (needs `make serve`/`make up-app` running) — the one that actually attacks each documented endpoint's input handling, same "dynamic pass against the live app" tier as `make strix-app`, just OWASP's own standard scanner instead of an autonomous agent. Only ever run against a target you own or have explicit permission to test — same rule `make strix-app` states for itself.
	mkdir -p zap_reports
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-api-scan.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000}/openapi.json -f openapi \
		-x api-scan.xml -r api-scan.html -I

zap-view:  ## Open the most recently modified ZAP HTML report (from `make zap-baseline`/`make zap-api-scan`)
	open "$$(ls -t zap_reports/*.html | head -1)" 2>/dev/null || xdg-open "$$(ls -t zap_reports/*.html | head -1)"

defectdojo-up:  ## Start a PERSISTENT self-hosted DefectDojo (http://localhost:8080) — where `make defectdojo-import` sends ZAP/Trivy/Semgrep/Checkov findings to get deduplicated and tracked across runs, the same "opt-in, persistent, cross-run history" role `make sonar-up` plays for SonarQube above. First run clones the pinned release ($(DEFECTDOJO_VERSION)) into $(DEFECTDOJO_DIR) (NOT into this repo — see this file's own header comment) and pulls DefectDojo's prebuilt images (no `docker compose build` needed — verified directly the checked-in docker-compose.yml's own `image:` lines are enough); every run after that just restarts the same instance. Prints the initializer's one-time-generated admin password (user: admin) — SAVE IT, it isn't shown again (short of digging through `docker compose logs` in $(DEFECTDOJO_DIR) yourself).
	@mkdir -p $(HOME)/.cache
	@test -d "$(DEFECTDOJO_DIR)" || git clone --branch $(DEFECTDOJO_VERSION) --depth 1 https://github.com/DefectDojo/django-DefectDojo.git "$(DEFECTDOJO_DIR)"
	cd "$(DEFECTDOJO_DIR)" && DJANGO_VERSION=$(DEFECTDOJO_VERSION) NGINX_VERSION=$(DEFECTDOJO_VERSION) docker compose up -d
	@echo "Waiting for the initializer (first run: DB migration + seed data, up to ~3 min)..."
	@for i in $$(seq 1 60); do \
		cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep -q "Admin password:" && break; \
		sleep 5; \
	done
	@cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep "Admin password:" || echo "(already initialized on a previous run — admin password was only ever printed once; see DefectDojo's own docs to reset it if lost)"
	@echo "DefectDojo: http://localhost:8080 (user: admin) — generate an API v2 Key under My Account for \`make defectdojo-import\`"

defectdojo-down:  ## Stop the persistent DefectDojo server (keeps its volumes — findings/history survive)
	cd "$(DEFECTDOJO_DIR)" && docker compose stop

defectdojo-import:  ## Import one scan report into the running `make defectdojo-up` instance (scripts/defectdojo_import.py) — needs DEFECTDOJO_API_KEY (My Account -> API v2 Key in the UI) plus REPORT=<path> SCAN_TYPE="<DefectDojo scan_type string>" [ENGAGEMENT=<name>]. E.g.: `DEFECTDOJO_API_KEY=... make defectdojo-import REPORT=zap_reports/baseline-report.xml SCAN_TYPE="ZAP Scan"` — auto-creates the Product/Engagement on first import, no manual UI setup needed.
	@test -n "$$DEFECTDOJO_API_KEY" || (echo "DEFECTDOJO_API_KEY is required — generate one in the DefectDojo UI (My Account -> API v2 Key)" && exit 1)
	@test -n "$$REPORT" || (echo "REPORT=<path to scan report file> is required" && exit 1)
	@test -n "$$SCAN_TYPE" || (echo 'SCAN_TYPE="<DefectDojo scan_type string>" is required, e.g. SCAN_TYPE="ZAP Scan"' && exit 1)
	python -m scripts.defectdojo_import --file "$$REPORT" --scan-type "$$SCAN_TYPE" $${ENGAGEMENT:+--engagement-name "$$ENGAGEMENT"}

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
	docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d \
		api agent-worker agent-worker-support agent-worker-ops agent-worker-sales ingest-worker

loadtest-queued:  ## Interactive Locust UI against the queued path (loadtest/locustfile_queued.py, the only HTTP chat path this app serves) — needs `make loadtest-up` first (or the host-native equivalent from that file's own docstring); measures app/turns/agent_worker.py's own concurrency, not native Ollama's
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000

loadtest-queued-headless:  ## Fixed 20-user, 2-minute headless run of the queued-path scenario above → CSV + HTML report under loadtest/results-queued/
	mkdir -p loadtest/results-queued
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000 \
		--headless --users 20 --spawn-rate 5 --run-time 2m \
		--csv loadtest/results-queued/loadtest --html loadtest/results-queued/report.html

strix:  ## Autonomous AI pentest of this repo's SOURCE (static) — needs Docker, `pipx install strix-agent` once (pipx isolates it from this repo's own .venv, the pip-conflict concern `make garak` solves with a second venv instead), and a CLOUD LLM key (STRIX_LLM/LLM_API_KEY — NOT the local Ollama the rest of this app runs on). Only ever point it at a target you own or have written authorization to test.
	strix --target . --scan-mode quick

strix-app:  ## Same tool, but black-box against the RUNNING app + its OpenAPI spec (needs `make up` + `make serve`/`make up-app`, and `make ingest` for real data to probe) — dynamic testing, not just a source read
	strix --target http://localhost:8000/openapi.json --target http://localhost:8000 --scan-mode quick

strix-view:  ## Open the local dashboard (findings, repro steps, agent graph) for the most recent `make strix`/`make strix-app` run
	strix view

logs:  ## Tail logs from all services
	docker compose logs -f

down:  ## Stop all services (keep volumes)
	docker compose down

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	docker compose exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

clear-streams:  ## Delete every Redis Stream (agent:requests:*, agent:results:*, ingest:requests, ingest:results:*) — drops queued/in-flight turns and ingest jobs; leaves the semantic cache and other keys intact
	docker compose exec redis sh -c "redis-cli --scan --pattern '*' | while read -r k; do [ \"\$$(redis-cli type \"\$$k\")\" = stream ] && redis-cli del \"\$$k\"; done"

clear-checkpoints:  ## Truncate the LangGraph checkpointer's tables (checkpoints/checkpoint_blobs/checkpoint_writes) in its dedicated `checkpointer` Postgres DB — drops all saved conversation state; leaves appdata/langfuse/litellm DBs and the migrations tracking table intact
	docker compose exec postgres psql -U langfuse -d checkpointer -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"

clear-langfuse:  ## Truncate Langfuse's own telemetry tables (traces, observations incl. generations, scores, trace_sessions, events, comments, media) in the `langfuse` Postgres DB, CASCADE (also clears dependent job_executions rows) — leaves projects/api_keys/users/models/pricing config intact so LANGFUSE_PUBLIC_KEY/SECRET_KEY keep working
	docker compose exec postgres psql -U langfuse -d langfuse -c "TRUNCATE traces, observations, scores, trace_sessions, events, comments, media, trace_media, observation_media CASCADE;"

clear-litellm:  ## Truncate LiteLLM's own usage/spend logs (SpendLogs + its tool/guardrail indexes, ErrorLogs, AuditLog, every Daily*Spend/Metrics table) in the `litellm` Postgres DB, CASCADE — leaves users/teams/keys/model config intact so the proxy and its admin UI (`make up` → http://localhost:4000/ui) keep working
	docker compose exec postgres psql -U langfuse -d litellm -c "TRUNCATE \"LiteLLM_SpendLogs\", \"LiteLLM_SpendLogToolIndex\", \"LiteLLM_SpendLogGuardrailIndex\", \"LiteLLM_ErrorLogs\", \"LiteLLM_AuditLog\", \"LiteLLM_DailyUserSpend\", \"LiteLLM_DailyTeamSpend\", \"LiteLLM_DailyTagSpend\", \"LiteLLM_DailyEndUserSpend\", \"LiteLLM_DailyOrganizationSpend\", \"LiteLLM_DailyAgentSpend\", \"LiteLLM_DailyToolSpend\", \"LiteLLM_DailyGuardrailMetrics\", \"LiteLLM_DailyPolicyMetrics\" CASCADE;"

clear-all: clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm  ## Run every clear-* target above in one shot — semantic cache, Redis Streams, checkpointer state, Langfuse telemetry, LiteLLM usage/spend logs. Same per-target scope/exclusions as running each individually (see each target's own description); does NOT touch appdata (employees/tickets/leads/incidents/usage_ledger) or delete any volume — for that, `make clean`

clean:  ## Stop services and delete volumes (models, vectors, traces)
	docker compose down -v

obs-up:  ## Start the observability stack (Grafana :3300, Prometheus :9090, Loki, Alertmanager, otel-collector) — independent of `make up`
	docker compose -f docker-compose.observability.yml up -d

obs-down:  ## Stop the observability stack (keep its volumes)
	docker compose -f docker-compose.observability.yml down

obs-logs:  ## Tail logs from the observability stack
	docker compose -f docker-compose.observability.yml logs -f

obs-clean:  ## Stop the observability stack and delete its volumes (Prometheus/Loki/Grafana data)
	docker compose -f docker-compose.observability.yml down -v
