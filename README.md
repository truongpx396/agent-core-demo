# practice-core-ai-1 — Local Core AI Stack Demo

A tiny, **fully offline** project that lets you grab the core, frequently-used
features of four popular AI-infra tools in one place:

| Tool          | What this demo shows |
|---------------|----------------------|
| **LangGraph** | Typed state, nodes, conditional edges, a tool-calling **agent loop**, a **durable checkpointer** (`AsyncSqliteSaver`; `MemorySaver` for tests) giving both **memory** and a human-approval pause that survives a restart, and **streaming** |
| **LiteLLM**   | An OpenAI-compatible **proxy** routing chat + embeddings to Ollama, with **retries**, **fallbacks**, and **Langfuse logging at the proxy** |
| **Qdrant**    | Collection creation, **batch upsert with payloads**, vector search, and **metadata filtering** |
| **Langfuse**  | `@observe` tracing, the LangGraph **callback handler**, nested spans, and **session grouping** by `thread_id` |
| **FastAPI + Pydantic** | An HTTP `/chat` API over the same agent, with typed request/response models and auto-generated OpenAPI docs |

Also included: **multi-layer safety budgets** (iteration/tool-call/token/tool-timeout/request-timeout caps), **Prometheus metrics** at `GET /metrics`, a **pytest suite** that runs the graph against a fake LLM (no live services needed), and a **golden-dataset evaluation harness** (`app/eval.py`) that runs against the real model to catch behavior regressions. See [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) for the full writeup of every pattern in `app/graph.py`.

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
- `remember that our refund window is 30 days, under the company topic` →
  uses **add_note**, the one *mutating* tool — it always pauses for human
  approval first, regardless of any flag, since it writes to the knowledge
  base (see GRAPH_PATTERNS.md pattern 15). Plain `make chat` has no way to
  answer that prompt, so it auto-declines and the agent explains why; run
  `make chat-stream` instead to actually see and approve/reject it (`y`/`N`)


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

`POST /chat` is single-shot — if the agent calls `add_note` (the one
mutating tool, always gated — see GRAPH_PATTERNS.md pattern 15), there's no
way for this endpoint to show an approval prompt, so it auto-declines and
returns the agent's response to that. `POST /chat/stream` surfaces the
pause as an `approval_required` SSE event instead (no `/chat/resume`
endpoint yet to act on it from HTTP — see GRAPH_PATTERNS.md's "Extending
Further"); `make chat-stream` and `app/hitl_demo.py` are the two places
that actually drive the approve/reject prompt end to end today.

- `GET /metrics` → Prometheus text format (tool calls, retries, HITL decisions,
  capability-gate hits, checkpoint issues, request latency/outcome — see
  `app/metrics.py`)

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
| `make test`       | Run the pytest suite (fake LLM, no live services needed) |
| `make eval`       | Run the golden-dataset evaluation against the real stack |
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
| `app/tools.py`         | `search_docs` + `calculator` (read-only) + `add_note` (the one mutating tool) — each wrapped with a timeout budget, each declaring a capability in `TOOL_CAPABILITIES` |
| `app/graph.py`         | LangGraph agent (state, edges, memory, safety budgets, mandatory capability gate, checkpoint version stamping) |
| `app/agent.py`         | Shared runtime (used by both CLI and API); request-level timeout + metrics recording; durable-checkpointer init (`init_graph_sync`/`init_graph_async`) |
| `app/metrics.py`       | Prometheus counters/histograms + the tool-call callback handler |
| `app/eval.py`          | Golden-dataset evaluation harness (`make eval`) |
| `app/chat.py`          | Streaming CLI + Langfuse tracing |
| `app/hitl_demo.py`     | Runnable HITL pause/resume demo against the shared durable graph (`python -m app.hitl_demo "..."`) |
| `app/schemas.py`       | Pydantic request/response models |
| `app/api.py`           | FastAPI service (`/health`, `/chat`, `/chat/stream`, `/metrics`) |
| `tests/`               | pytest suite — routing/node/graph/tool/checkpointer tests against a fake LLM and a real tmp-file SQLite checkpointer (`make test`, no live services) |

## Troubleshooting

- **`make ingest` fails to embed** → run `make up` and `make pull-models` first.
- **No traces in Langfuse** → make sure you put the keys in `.env` and ran
  `docker compose up -d litellm` afterwards; also restart `make chat`.
- **Model too slow** → `qwen2.5:3b` is small; swap for a bigger
  model in `litellm-config.yaml` if you have the RAM.
