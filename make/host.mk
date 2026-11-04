# Targets that run the application's own processes directly on this machine (the API, the queue consumers, the channels, the MCP servers),
# as opposed to in a container: `make up` starts what they talk to, and `make up-app` / `make up-all` run containerized versions of the API and the workers.
# Included by the Makefile, so `make serve`, `make agent-worker` and the rest are called exactly as before, and `make help` lists them with everything else.

# --- The API and the MCP servers -------------------------------------------------------------------------------------------------------------------

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

mcp-serve:  ## Start the MCP server exposing query_employees (stdio transport; needs `make up`, and MCP_TENANT + MCP_PRINCIPAL in .env or exported: the identity it acts for, never a tool argument)
	python -m app.mcp.server

mcp-inspect:  ## Launch the MCP Inspector against app/mcp/server.py for interactive testing (set MCP_TENANT + MCP_PRINCIPAL first, as for mcp-serve)
	mcp dev app/mcp/server.py

mcp-serve-ops:  ## Start the MCP server exposing fetch_metrics_summary/list_recent_incidents (stdio transport; needs `make up`, and MCP_PRINCIPAL in .env or exported) — see app/mcp/ops_server.py
	python -m app.mcp.ops_server

# --- Queue consumers: the agent workers (one pool per domain), the ingest worker and the billing and meter reconcile loops -------------------------

agent-worker:  ## Start a Redis Streams agent worker for the Ecorp domain (run several for independent scaling). Every chat front door publishes to it: POST /chat/stream/queued, `make chat` and `make telegram`
	python -m app.job_queue.agent_worker

agent-worker-support:  ## Start an agent worker pool for the Tier-1 support domain (see app/domains/support/); the web UI's X-Domain: support turns route here
	AGENT_DOMAIN=support python -m app.job_queue.agent_worker

agent-worker-ops:  ## Start an agent worker pool for the ops domain (see app/domains/ops/); the web UI's X-Domain: ops turns route here
	AGENT_DOMAIN=ops python -m app.job_queue.agent_worker

agent-worker-sales:  ## Start an agent worker pool for the sales/CRM domain (see app/domains/sales/); the web UI's X-Domain: sales turns route here
	AGENT_DOMAIN=sales python -m app.job_queue.agent_worker

ingest-worker:  ## Start a document-ingestion worker (PDF/DOCX uploads; run several for independent scaling; see POST /ingest/upload)
	python -m app.ingestion.ingest_worker

billing-export-worker:  ## Drain billing_export_outbox to the enabled providers that bill on usage, in a loop (the billing package's credit-billing-export-worker; `ARGS=--once` for a single pass; set BILLING_PROVIDERS the same as the API and agent workers)
	python -m credit_billing.cli.export_worker $(ARGS)

credit-reconcile-worker:  ## The same check as a worker: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, re-publishing the drift and balance gauges every minute for the dashboard and alerts
	python -m credit_billing.cli.reconcile --loop $(ARGS)

usage-reconcile-worker:  ## The same comparison as a worker: a pass every USAGE_RECONCILE_INTERVAL_SECONDS, re-publishing the drift gauge every minute for the dashboard and alerts (see scripts/usage_reconcile.py)
	python -m scripts.usage_reconcile --loop $(ARGS)

# --- Channels: the interactive CLI and the Telegram bots (each reaches the API through the front door and needs the matching worker) ---------------

chat:  ## Start the interactive agent CLI (shows tool calls). Each turn goes through the queue, so it needs `make up` and a worker for its domain (`make agent-worker`); AGENT_DOMAIN=support|ops|sales picks another pool
	python -m app.channels.chat

chat-hitl:  ## Interactive CLI with human-in-the-loop tool approval (the answer is a resume job on the queue; same worker requirement as `make chat`)
	python -m app.channels.chat --hitl

telegram:  ## Start the Telegram bot channel for the Ecorp domain (needs TELEGRAM_BOT_TOKEN in .env, and `make agent-worker` running: messages reach the API through the front door; a turn that needs approval shows Approve/Reject buttons; see app/channels/telegram.py and specs/021)
	python -m app.channels.telegram

telegram-support:  ## Start the Telegram channel as the Tier-1 support copilot (see app/domains/support/); needs `make agent-worker-support` running
	AGENT_DOMAIN=support python -m app.channels.telegram

telegram-sales:  ## Start the Telegram channel as the sales/CRM concierge (see app/domains/sales/); needs `make agent-worker-sales` running
	AGENT_DOMAIN=sales python -m app.channels.telegram

# --- Host-process helpers --------------------------------------------------------------------------------------------------------------------------

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
	pkill -f 'app\.job_queue\.agent_worker' 2>/dev/null || true
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
		lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 || pgrep -f 'app\.job_queue\.agent_worker' >/dev/null 2>&1 || pgrep -f 'app\.ingestion\.ingest_worker' >/dev/null 2>&1 || break; \
		sleep 0.5; \
	done
	lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	pkill -9 -f 'app\.job_queue\.agent_worker' 2>/dev/null || true
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
