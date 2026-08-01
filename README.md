# practice-core-ai-1 — Local Core AI Stack Demo

A tiny, **fully offline** project that lets you grab the core, frequently-used
features of four popular AI-infra tools in one place:

| Tool          | What this demo shows |
|---------------|----------------------|
| **LangGraph** | Typed state, nodes, conditional edges, a tool-calling **agent loop**, `MemorySaver` **memory**, and **streaming** |
| **LiteLLM**   | An OpenAI-compatible **proxy** routing chat + embeddings to Ollama, with **retries**, **fallbacks**, and **Langfuse logging at the proxy** |
| **Qdrant**    | Collection creation, **batch upsert with payloads**, vector search, and **metadata filtering** |
| **Langfuse**  | `@observe` tracing, the LangGraph **callback handler**, nested spans, and **session grouping** by `thread_id` |

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

## Ports

| Service      | URL                     |
|--------------|-------------------------|
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
| `make logs`       | Tail service logs |
| `make down`       | Stop services (keep data) |
| `make clean`      | Stop services and delete volumes |

## File tour

| File | Responsibility |
|------|----------------|
| `docker-compose.yml`   | All 5 services |
| `litellm-config.yaml`  | Model routing, retries, fallbacks, Langfuse callback |
| `app/config.py`        | Env-driven settings |
| `app/sample_docs.py`   | Sample knowledge base |
| `app/embeddings.py`    | Embedding client (via LiteLLM) |
| `app/qdrant_store.py`  | Qdrant collection / upsert / filtered search |
| `app/ingest.py`        | Embed + load docs |
| `app/tools.py`         | `search_docs` + `calculator` tools |
| `app/graph.py`         | LangGraph agent (state, edges, memory) |
| `app/chat.py`          | Streaming CLI + Langfuse tracing |

## Troubleshooting

- **`make ingest` fails to embed** → run `make up` and `make pull-models` first.
- **No traces in Langfuse** → make sure you put the keys in `.env` and ran
  `docker compose up -d litellm` afterwards; also restart `make chat`.
- **Model too slow** → `qwen2.5:3b` is small; swap for a bigger
  model in `litellm-config.yaml` if you have the RAM.
