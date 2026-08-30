.PHONY: help up up-app pull-models ingest index-skills chat chat-stream chat-stream-hitl serve mcp-serve telegram telegram-support telegram-sales agent-worker ingest-worker ops-digest followup-sweep test lint typecheck eval logs down clean clear-cache obs-up obs-down obs-logs obs-clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start all services (ollama, litellm, qdrant, langfuse, postgres, minio)
	docker compose up -d

up-app:  ## Start infra + the containerized app itself (api, agent-worker, ingest-worker; see Dockerfile)
	docker compose --profile app up -d --build

pull-models:  ## Download the Ollama chat + embedding models
	docker compose exec ollama ollama pull qwen2.5:3b
	docker compose exec ollama ollama pull nomic-embed-text

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m scripts.seed

index-skills:  ## Embed the skills/ catalog (name+description) and upsert into its own Qdrant collection
	python -m scripts.index_skills

chat:  ## Start the interactive LangGraph agent CLI (dev streaming)
	python -m app.channels.chat

chat-stream:  ## Start the production streaming CLI (astream_events v2, shows tool calls)
	python -m app.channels.chat --stream

chat-stream-hitl:  ## Production streaming CLI with human-in-the-loop tool approval
	python -m app.channels.chat --stream --hitl

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

telegram:  ## Start the Telegram bot channel for the Acme domain (needs TELEGRAM_BOT_TOKEN in .env; see app/channels/telegram.py)
	python -m app.channels.telegram

telegram-support:  ## Start the Telegram channel as the Tier-1 support copilot (see app/domains/support/)
	AGENT_DOMAIN=support python -m app.channels.telegram

telegram-sales:  ## Start the Telegram channel as the sales/CRM concierge (see app/domains/sales/)
	AGENT_DOMAIN=sales python -m app.channels.telegram

agent-worker:  ## Start a Redis Streams agent worker (run several for independent scaling; see POST /chat/stream/queued)
	python -m app.turns.agent_worker

ingest-worker:  ## Start a document-ingestion worker (PDF/DOCX uploads; run several for independent scaling; see POST /ingest/upload)
	python -m app.ingestion.ingest_worker

ops-digest:  ## Run the ops bot's one-shot metrics digest, posting to the team channel (see scripts/ops_digest.py; meant for real cron)
	python -m scripts.ops_digest

followup-sweep:  ## Run the sales concierge's one-shot due-follow-up sweep, drafting nudges for a human to review (see scripts/followup_sweep.py; meant for real cron)
	python -m scripts.followup_sweep

test:  ## Run the graph test suite (no live services needed — fake LLM, no Qdrant)
	pytest -q

lint:  ## Static checks: ruff (style/correctness) — see pyproject.toml's [tool.ruff]
	ruff check .

typecheck:  ## Static checks: mypy over app/ and scripts/ — see pyproject.toml's [tool.mypy]
	mypy

eval:  ## Run the golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.eval

logs:  ## Tail logs from all services
	docker compose logs -f

down:  ## Stop all services (keep volumes)
	docker compose down

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	docker compose exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

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
