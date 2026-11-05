# The app's own processes on this machine, not in containers. Included by the Makefile.

# --- API and MCP servers ---

serve:  ## Start the FastAPI service (http://localhost:8000/docs)
	# --reload-dir app: uvicorn otherwise watches the whole tree incl. .venv (17k+ files), a steady CPU load
	# that slowed chat turns next to Ollama.
	uvicorn app.adapters.inbound.api.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app

mcp-serve:  ## MCP server exposing query_employees over stdio; needs `make up` and MCP_TENANT + MCP_PRINCIPAL (the identity it acts for) in .env or exported
	python -m app.adapters.inbound.mcp.server

mcp-inspect:  ## MCP Inspector against app/adapters/inbound/mcp/server.py (set MCP_TENANT + MCP_PRINCIPAL first)
	mcp dev app/adapters/inbound/mcp/server.py

mcp-serve-ops:  ## MCP server exposing fetch_metrics_summary/list_recent_incidents over stdio; needs `make up` and MCP_PRINCIPAL
	python -m app.adapters.inbound.mcp.ops_server

# --- Queue consumers ---

agent-worker:  ## Redis Streams agent worker for the Ecorp domain (run several to scale); AGENT_WORKER_DOMAINS=support,ops picks others
	python -m app.adapters.inbound.workers.agent_worker

agent-worker-all:  ## One agent worker serving every domain (ecorp, support, ops, sales)
	AGENT_WORKER_DOMAINS=all python -m app.adapters.inbound.workers.agent_worker

ingest-worker:  ## Document-ingestion worker for POST /ingest/upload (run several to scale)
	python -m app.adapters.inbound.workers.ingest_worker

billing-export-worker:  ## Drain billing_export_outbox to the providers that bill on usage, in a loop (ARGS=--once for one pass; set BILLING_PROVIDERS as for the API and workers)
	python -m credit_billing.cli.export_worker $(ARGS)

credit-reconcile-worker:  ## credit-reconcile as a loop: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, gauges refreshed every minute
	python -m credit_billing.cli.reconcile --loop $(ARGS)

usage-reconcile-worker:  ## usage-reconcile as a loop: a pass every USAGE_RECONCILE_INTERVAL_SECONDS, drift gauge refreshed every minute
	python -m scripts.usage_reconcile --loop $(ARGS)

# --- Channels ---

chat:  ## Interactive agent CLI; turns go through the queue, so it needs `make up` and a worker (`make agent-worker`); AGENT_DOMAIN=support|ops|sales picks another domain (`make agent-worker-all`)
	python -m app.adapters.inbound.channels.chat

chat-hitl:  ## Interactive CLI with tool approval (same worker requirement as `make chat`)
	python -m app.adapters.inbound.channels.chat --hitl

telegram:  ## Telegram bot for the Ecorp domain; needs TELEGRAM_BOT_TOKEN and `make agent-worker`; approvals appear as Approve/Reject buttons (specs/021)
	python -m app.adapters.inbound.channels.telegram

telegram-support:  ## Telegram bot as the support copilot; needs a worker serving support (`make agent-worker-all`)
	AGENT_DOMAIN=support python -m app.adapters.inbound.channels.telegram

telegram-sales:  ## Telegram bot as the sales concierge; needs a worker serving sales (`make agent-worker-all`)
	AGENT_DOMAIN=sales python -m app.adapters.inbound.channels.telegram

# --- Helpers ---

restart-all:  ## Kill and relaunch the API, one agent worker for every domain and the ingest worker as background host processes (logs in var/), then `make ingest` and `make index-skills`. Host dev only, not for `up-app`
	pkill -f 'uvicorn app\.adapters\.inbound\.api\.main:app' 2>/dev/null || true
	# `serve --reload` runs the real server as a multiprocessing child that the pkill above never matches; if its
	# reloader parent died, the child stays orphaned on :8000 serving stale code. Kill whatever holds the port.
	lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
	pkill -f 'app\.adapters\.inbound\.workers\.agent_worker' 2>/dev/null || true
	pkill -f 'app\.adapters\.inbound\.workers\.ingest_worker' 2>/dev/null || true
	# A killed process lingers: a fixed `sleep 1` twice left a stale worker running beside a new duplicate (two
	# consumers on one Redis group) and kept a code change from taking effect. Poll for exit; SIGKILL after 10s.
	for i in $$(seq 1 20); do \
		lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 || pgrep -f 'app\.adapters\.inbound\.workers\.agent_worker' >/dev/null 2>&1 || pgrep -f 'app\.adapters\.inbound\.workers\.ingest_worker' >/dev/null 2>&1 || break; \
		sleep 0.5; \
	done
	lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	pkill -9 -f 'app\.adapters\.inbound\.workers\.agent_worker' 2>/dev/null || true
	pkill -9 -f 'app\.adapters\.inbound\.workers\.ingest_worker' 2>/dev/null || true
	mkdir -p var
	nohup $(MAKE) serve > var/serve.log 2>&1 &
	nohup $(MAKE) agent-worker-all > var/agent-worker.log 2>&1 &
	nohup $(MAKE) ingest-worker > var/ingest-worker.log 2>&1 &
	$(MAKE) ingest
	$(MAKE) index-skills

fake-llm:  ## Fake concurrent LLM (loadtest/fake_llm_server.py, :9009) to load-test agent_worker's concurrency without Ollama's -np 1 ceiling
	uvicorn loadtest.fake_llm_server:app --host 0.0.0.0 --port 9009
