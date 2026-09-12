.PHONY: help up up-app sandbox-up sandbox-build pull-models ingest index-skills chat chat-hitl serve mcp-serve mcp-serve-ops telegram telegram-support telegram-sales agent-worker agent-worker-support agent-worker-ops agent-worker-sales restart-all fake-llm ingest-worker ops-digest followup-sweep test test-integration test-live test-sandbox lint typecheck eval promptfoo promptfoo-redteam deepeval garak garak-full trivy trivy-image loadtest-queued loadtest-queued-headless strix strix-app strix-view logs down clean clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm clear-all obs-up obs-down obs-logs obs-clean

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

test-integration:  ## Real Postgres/Redis/Qdrant via testcontainers (no LLM) — needs Docker, no `make up` required (GRAPH_PATTERNS.md pattern 48)
	# --dist=loadgroup: tests/integration/test_worker_scaling.py's own
	# xdist_group marker needs this to actually take effect (plain `load`
	# ignores it) — see that module's own comment for why.
	pytest -n auto -m integration -q --dist=loadgroup

test-live:  ## Real small Ollama model + full app/agent-worker stack via testcontainers, incl. Playwright browser E2E and real crawl4ai renders — needs Docker (pattern 48/50)
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

promptfoo-redteam:  ## Adversarial variants of the support prompt (prompt injection, policy violations), generated+graded locally by Ollama — see promptfoo/redteam.yaml's own comments for real, disclosed limits on how far that local generation/grading can be trusted
	npm install --include=optional
	python -m promptfoo.dump_prompts
	# `redteam run` REWRITES its --config file in place with the generated+
	# graded test suite baked in (confirmed directly: it clobbered the
	# checked-in redteam.yaml twice during development) — copying to a
	# scratch file first (same directory, so redteam.yaml's own
	# `file://prompts/support.json` still resolves) keeps the hand-authored
	# source under version control intact across repeated runs.
	cp promptfoo/redteam.yaml promptfoo/.redteam-run-scratch.yaml
	PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config promptfoo/.redteam-run-scratch.yaml

deepeval:  ## LLM-judged RAG quality (tests/live/test_rag_quality_deepeval.py) + a multi-turn conversation simulation (test_conversation_simulator_deepeval.py) against the real graph — needs Docker; read the printed reasons by hand, don't trust pass/fail alone (see those files' own disclosed judge-reliability findings, GRAPH_PATTERNS.md pattern 48)
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s

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
	docker run --rm -v $(PWD):/repo aquasec/trivy:0.74.0 fs \
		--scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed \
		--file-patterns 'pip:requirements-lock\.txt$$' \
		--skip-dirs .venv,node_modules,.git,.mypy_cache,.ruff_cache,.pytest_cache /repo

trivy-image:  ## Build the app image (see Dockerfile) and scan it for OS/library vulnerabilities
	docker build -t agent-core-demo:trivy .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 image \
		--severity HIGH,CRITICAL --ignore-unfixed agent-core-demo:trivy

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
