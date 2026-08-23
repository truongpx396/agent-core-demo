.PHONY: help up pull-models ingest chat chat-stream chat-stream-hitl serve mcp-serve telegram agent-worker test eval logs down clean clear-cache

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start all services (ollama, litellm, qdrant, langfuse, postgres)
	docker compose up -d

pull-models:  ## Download the Ollama chat + embedding models
	docker compose exec ollama ollama pull qwen2.5:3b
	docker compose exec ollama ollama pull nomic-embed-text

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m app.ingest

chat:  ## Start the interactive LangGraph agent CLI (dev streaming)
	python -m app.chat

chat-stream:  ## Start the production streaming CLI (astream_events v2, shows tool calls)
	python -m app.chat --stream

chat-stream-hitl:  ## Production streaming CLI with human-in-the-loop tool approval
	python -m app.chat --stream --hitl

serve:  ## Start the FastAPI service (http://localhost:8000/docs)
	# --reload-dir scopes the file watcher to app/ only. Without it, uvicorn
	# watches the ENTIRE working directory recursively, including .venv —
	# 17k+ files here vs ~150 in app/ — and continuously re-scans that whole
	# tree (verified empirically via `sample`: hundreds of lstat/open/
	# getdirentries syscalls per second). That's real, sustained CPU
	# competing directly with Ollama's own CPU-bound inference in this
	# docker-compose stack — on a resource-constrained machine it was
	# measurably slowing down/timing out ordinary chat turns.
	uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload --reload-dir app

mcp-serve:  ## Start the MCP server exposing query_employees (stdio transport; needs `make up`)
	python -m app.mcp_server

mcp-inspect:  ## Launch the MCP Inspector against app/mcp_server.py for interactive testing
	mcp dev app/mcp_server.py

telegram:  ## Start the Telegram bot channel (needs TELEGRAM_BOT_TOKEN in .env; see app/telegram_channel.py)
	python -m app.telegram_channel

agent-worker:  ## Start a Redis Streams agent worker (run several for independent scaling; see POST /chat/stream/queued)
	python -m app.agent_worker

test:  ## Run the graph test suite (no live services needed — fake LLM, no Qdrant)
	pytest -q

eval:  ## Run the golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m app.eval

logs:  ## Tail logs from all services
	docker compose logs -f

down:  ## Stop all services (keep volumes)
	docker compose down

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	docker compose exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

clean:  ## Stop services and delete volumes (models, vectors, traces)
	docker compose down -v
