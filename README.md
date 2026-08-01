# practice-core-ai-1 — Local Core AI Stack Demo

A tiny, **fully offline** project that lets you grab the core, frequently-used
features of four popular AI-infra tools in one place:

| Tool          | What this demo shows |
|---------------|----------------------|
| **LangGraph** | Typed state, nodes, conditional edges, a tool-calling **agent loop**, `MemorySaver` **memory**, and **streaming** |
| **LiteLLM**   | An OpenAI-compatible **proxy** routing chat + embeddings to Ollama, with **retries**, **fallbacks**, and **Langfuse logging at the proxy** |
| **Qdrant**    | Collection creation, **batch upsert with payloads**, vector search, and **metadata filtering** |
| **Langfuse**  | `@observe` tracing, the LangGraph **callback handler**, nested spans, and **session grouping** by `thread_id` |
| **FastAPI + Pydantic** | An HTTP `/chat` API over the same agent, with typed request/response models and auto-generated OpenAPI docs |

Everything runs locally via **Ollama** — no cloud API keys needed.

## Architecture

```
  make chat / make ingest  (host Python app)
        │  OpenAI API
        ▼
   LiteLLM proxy ───► Ollama (qwen2.5:3b + nomic-embed-text)
        │  logs
        ▼
     Langfuse ◄──── app traces (callback handler)
        ▲
   Qdrant (vectors)  ◄── ingest / search_docs tool
```

The app is a **RAG agent**: it retrieves from ingested docs (Qdrant) and can
call tools (ReAct-style) via a LangGraph agent, all traced in Langfuse.

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- ~2 GB disk for the Ollama models (first run only)

## Quickstart

```bash
cd practice-core-ai-1

# 1. Config + Python deps
cp .env.example .env
pip install -r requirements.txt

# 2. Start the stack (ollama, litellm, qdrant, langfuse, postgres)
make up

# 3. Pull the local models (first run only, ~1-2 GB)
make pull-models

# 4. Get Langfuse keys: open http://localhost:3000, create an account +
#    project, copy the public/secret keys into .env, then restart litellm:
docker compose up -d litellm

# 5. Load sample docs into Qdrant
make ingest

# 6. Chat with the agent
make chat
```

Try these in the chat:
- `What is a LangGraph checkpointer?`  → uses **search_docs** (retrieval)
- `What are Acme Corp support hours?`  → retrieval with a **topic** the agent can filter on
- `what is 21 * 2?`                    → uses the **calculator** tool
- Ask a follow-up like `and what did I just ask?` → shows **memory**

Then open **http://localhost:3000** to see the traces.

## HTTP API (FastAPI + Pydantic)

Besides the CLI, the same agent is exposed as an HTTP service:

```bash
make serve          # starts uvicorn on http://localhost:8000
```

- Interactive docs (Swagger UI): **http://localhost:8000/docs**
- `GET /health` → `{"status":"ok"}`
- `POST /chat` with a Pydantic-validated body:

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"what is 21 * 2?","thread_id":"demo"}'
# -> {"thread_id":"demo","answer":"The result of 21 * 2 is 42."}
```

Reuse the same `thread_id` across calls to keep conversation **memory**; each
call is also traced in Langfuse under that id as the session.

## Ports

| Service      | URL                     |
|--------------|-------------------------|
| FastAPI      | http://localhost:8000   |
| LiteLLM      | http://localhost:4000   |
| Qdrant       | http://localhost:6333   |
| Langfuse UI  | http://localhost:3000   |
| Ollama       | http://localhost:11434  |

## Make targets

| Target            | Description |
|-------------------|-------------|
| `make up`         | Start all services |
| `make pull-models`| Download Ollama chat + embedding models |
| `make ingest`     | Embed sample docs → Qdrant |
| `make chat`       | Start the agent CLI |
| `make serve`      | Start the FastAPI service (http://localhost:8000/docs) |
| `make logs`       | Tail service logs |
| `make down`       | Stop services (keep data) |
| `make clean`      | Stop services and delete volumes |

## File tour

| File | Responsibility |
|------|----------------|
| `docker-compose.yml`   | All 5 services |
| `litellm-config.yaml`  | Model routing, retries, fallbacks, Langfuse callback |
| `app/config.py`        | Typed settings (Pydantic `BaseSettings`) |
| `app/sample_docs.py`   | Sample knowledge base |
| `app/embeddings.py`    | Embedding client (via LiteLLM) |
| `app/qdrant_store.py`  | Qdrant collection / upsert / filtered search |
| `app/ingest.py`        | Embed + load docs |
| `app/tools.py`         | `search_docs` + `calculator` tools |
| `app/graph.py`         | LangGraph agent (state, edges, memory) |
| `app/agent.py`         | Shared runtime (used by both CLI and API) |
| `app/chat.py`          | Streaming CLI + Langfuse tracing |
| `app/schemas.py`       | Pydantic request/response models |
| `app/api.py`           | FastAPI service (`/health`, `/chat`) |

## Troubleshooting

- **`make ingest` fails to embed** → run `make up` and `make pull-models` first.
- **No traces in Langfuse** → make sure you put the keys in `.env` and ran
  `docker compose up -d litellm` afterwards; also restart `make chat`.
- **Model too slow** → `qwen2.5:3b` is small; swap for a bigger
  model in `litellm-config.yaml` if you have the RAM.
