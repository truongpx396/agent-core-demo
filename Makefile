.PHONY: help up pull-models ingest chat logs down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start all services (ollama, litellm, qdrant, langfuse, postgres)
	docker compose up -d

pull-models:  ## Download the Ollama chat + embedding models
	docker compose exec ollama ollama pull llama3.2:1b
	docker compose exec ollama ollama pull nomic-embed-text

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m app.ingest

chat:  ## Start the interactive LangGraph agent CLI
	python -m app.chat

logs:  ## Tail logs from all services
	docker compose logs -f

down:  ## Stop all services (keep volumes)
	docker compose down

clean:  ## Stop services and delete volumes (models, vectors, traces)
	docker compose down -v
