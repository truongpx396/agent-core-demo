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

Also included: **multi-tenant isolation** (`app/security.py` — every retrieval/write scoped to a `SecurityCtx` and enforced as a Qdrant pre-filter, never a Python post-filter), **cross-session memory** (write-gated, re-filtered on every recall), **hybrid retrieval with cited answers** (dense + BM25 sparse, RRF-fused, cross-encoder reranked — every claim in the answer traceable to a numbered source), a **fixed structured-data tool** (`query_employees`, also reachable over **MCP**) instead of a text-to-SQL surface, a **tenant+principal-scoped semantic cache** (Redis Stack vector KNN) that short-circuits repeat questions, **multi-layer safety budgets** (iteration/tool-call/token/tool-timeout/request-timeout caps), **Prometheus metrics** at `GET /metrics`, a **pytest suite** that runs the graph against a fake LLM (no live services needed), and a **golden-dataset evaluation harness** (`app/eval.py`) that runs against the real model to catch behavior regressions. See [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) for the full writeup of every pattern in `app/graph.py`.

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
   Qdrant (hybrid dense+BM25 vectors) ◄── ingest / search_docs / recall_memories
        ▲
   Postgres `appdata` DB ◄── query_employees tool / MCP server (app/mcp_server.py)
        ▲
   Redis Stack (vector KNN) ◄── semantic cache (check/write_semantic_cache nodes)
```

The app is a **RAG agent**: it retrieves from ingested docs (Qdrant, hybrid
search + rerank) and can call tools (ReAct-style, including a fixed
structured-data query against Postgres) via a LangGraph agent, all traced in
Langfuse — with a semantic cache in front to skip repeat work.

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- ~2 GB disk for the Ollama models (first run only)
- ~1 GB disk for the local hybrid-search/rerank models (`fastembed`'s BM25 +
  cross-encoder, downloaded once on first use, cached after)

## Quickstart

```bash
cd practice-core-ai-1

# 1. Config + Python deps
cp .env.example .env
pip install -r requirements.txt

# 2. Start the stack (ollama, litellm, qdrant, langfuse, postgres, redis)
#    A fresh postgres volume auto-runs postgres-init/*.sql, creating the
#    `appdata` DB query_employees reads (see postgres-init/02-appdata.sql).
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
- `What is a LangGraph checkpointer?`  → uses **search_docs** (hybrid dense+BM25
  retrieval, RRF-fused, cross-encoder reranked); the answer cites its
  sources inline (`[1]`, `[2]`, ...) — see GRAPH_PATTERNS.md pattern 20.
  Ask the exact same thing again (same or a different thread) and the
  second answer comes back near-instantly, served from the **semantic
  cache** instead of re-running retrieval + the LLM (pattern 22)
- `What are Acme Corp support hours?`  → retrieval with a **topic** the agent can filter on
- `what is 21 * 2?`                    → uses the **calculator** tool
- `Who works in Engineering at Acme?`  → uses **query_employees**, a fixed,
  typed query against Postgres — not a text-to-SQL tool (pattern 21); also
  reachable over MCP, see below
- `remember that our refund window is 30 days, under the company topic` →
  uses **add_note**, a *mutating* tool — it always pauses for human
  approval first, regardless of any flag, since it writes to the knowledge
  base (see GRAPH_PATTERNS.md pattern 15). Plain `make chat` has no way to
  answer that prompt, so it auto-declines and the agent explains why; run
  `make chat-stream` instead to actually see and approve/reject it (`y`/`N`)
- `remember that I prefer dark roast coffee` → uses **remember**, the other
  mutating tool — a personal, cross-session memory scoped to *you*
  specifically (by OS user, in the CLI), not the shared knowledge base
  `add_note` writes to. Also gated behind approval. Ask something related
  in a later session and the agent recalls it automatically — no tool call
  needed to *read* it back (GRAPH_PATTERNS.md pattern 18)
- Ask a follow-up like `and what did I just ask?` → shows **memory** (conversation-level, via `thread_id`)

Then open **http://localhost:3000** to see the traces.

## HTTP API (FastAPI + Pydantic)

Besides the CLI, the same agent is exposed as an HTTP service:

```bash
make serve          # starts uvicorn on http://localhost:8000
```

- Interactive docs (Swagger UI): **http://localhost:8000/docs**
- `GET /health` → `{"status":"ok"}`
- `POST /chat` with a Pydantic-validated body — and two **required** headers,
  `X-Tenant-Id`/`X-Principal-Id`, stamped into a `SecurityCtx`
  (`app/security.py`) that scopes every retrieval/write this request can see
  or touch (GRAPH_PATTERNS.md pattern 17). Omit either one and FastAPI
  rejects the request (422) before it reaches the graph at all — this is
  not authentication (nothing verifies the header values), it's the seam a
  real auth gateway plugs into; see `app/api.py`'s module docstring.

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: acme" \
  -H "X-Principal-Id: demo-user" \
  -d '{"message":"what is 21 * 2?","thread_id":"demo"}'
# -> {"thread_id":"demo","answer":"The result of 21 * 2 is 42."}
```

Use `X-Tenant-Id: acme` to see the docs `make ingest` seeded (`acme` is
`DEFAULT_TENANT` in `app/config.py`) — a different tenant id sees none of
them, by design.

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
  capability-gate hits, checkpoint issues, request latency/outcome, retrieval
  degradation, semantic cache hit/miss — see `app/metrics.py`)

`POST /chat`'s response also carries `citations`: the sources the answer
actually referenced (by bracket marker), not everything retrieved — see
`app/schemas.py::ChatResponse` and GRAPH_PATTERNS.md pattern 20. The
streaming endpoint emits the same data as a `{"type": "citations", ...}` SSE
event right before `done`.

## MCP server (`app/mcp_server.py`)

`query_employees` (GRAPH_PATTERNS.md pattern 21) is also reachable outside
this app's own LLM loop, over the Model Context Protocol — so an external
MCP client (Claude Desktop, another agent) can query it directly:

```bash
make mcp-serve      # stdio transport — how an MCP client launches this
make mcp-inspect     # interactive testing via the MCP Inspector
```

MCP has no equivalent of this app's trusted-header seam (`app/api.py`), so
`tenant`/`principal` are explicit tool arguments here — a demo
simplification documented in `app/mcp_server.py`'s module docstring, not a
weaker isolation guarantee: every call still goes through the same
`DEFAULT_POLICY` fail-closed check and the same mandatory `WHERE tenant =
%s` in `app/sql_store.py`.

## Ports

| Service          | URL                        |
|------------------|----------------------------|
| FastAPI          | http://localhost:8000      |
| LiteLLM          | http://localhost:4000      |
| Qdrant           | http://localhost:6333      |
| Langfuse UI      | http://localhost:3000      |
| Ollama           | http://localhost:11434     |
| Postgres         | localhost:5432 (`appdata` DB, `query_employees`) |
| Redis Stack      | localhost:6379 (semantic cache) |

## Make targets

| Target            | Description |
|-------------------|-------------|
| `make up`         | Start all services |
| `make pull-models`| Download Ollama chat + embedding models |
| `make ingest`     | Embed sample docs → Qdrant |
| `make chat`       | Start the agent CLI |
| `make serve`      | Start the FastAPI service (http://localhost:8000/docs) |
| `make mcp-serve`  | Start the MCP server exposing `query_employees` (stdio transport) |
| `make mcp-inspect`| Launch the MCP Inspector against `app/mcp_server.py` |
| `make test`       | Run the pytest suite (fake LLM, no live services needed) |
| `make eval`       | Run the golden-dataset evaluation against the real stack |
| `make logs`       | Tail service logs |
| `make down`       | Stop services (keep data) |
| `make clean`      | Stop services and delete volumes |

## File tour

| File | Responsibility |
|------|----------------|
| `docker-compose.yml`   | All 7 services |
| `litellm-config.yaml`  | Model routing, retries, fallbacks, Langfuse callback |
| `postgres-init/`       | SQL run automatically on a fresh postgres volume — `01-*.sql` (litellm/langfuse), `02-appdata.sql` (the `employees` table `query_employees` reads) |
| `app/config.py`        | Typed settings (Pydantic `BaseSettings`) |
| `app/sample_docs.py`   | Sample knowledge base |
| `app/embeddings.py`    | Dense embedding client (via LiteLLM) + local BM25 sparse embedding + cross-encoder reranker (`fastembed`) |
| `app/qdrant_store.py`  | Qdrant collection (named dense+sparse vectors) / upsert / `hybrid_search` (RRF fusion + rerank, with degradation) |
| `app/ingest.py`        | Embed + load docs |
| `app/sql_store.py`     | The one fixed, parameterized `query_employees` query against Postgres — never generated SQL (GRAPH_PATTERNS.md pattern 21) |
| `app/semantic_cache.py`| Tenant+principal-scoped semantic cache (Redis Stack vector KNN); degrades to a miss on any failure (pattern 22) |
| `app/security.py`      | `SecurityCtx` + `Policy` — tenant/owner isolation, enforced as a Qdrant pre-filter (GRAPH_PATTERNS.md pattern 17) |
| `app/tools.py`         | `search_docs` + `calculator` + `query_employees` (read-only) + `add_note` + `remember` (mutating) — each wrapped with a timeout budget, each declaring a capability in `TOOL_CAPABILITIES`; ctx-scoped via `app/security.py` |
| `app/graph.py`         | LangGraph agent (state, edges, memory, safety budgets, mandatory capability gate, checkpoint version stamping, SecurityCtx fail-closed guard, semantic cache short-circuit, citation extraction) |
| `app/agent.py`         | Shared runtime (used by both CLI and API); request-level timeout + metrics recording; durable-checkpointer init (`init_graph_sync`/`init_graph_async`) |
| `app/mcp_server.py`    | MCP server exposing `query_employees` over stdio (`make mcp-serve`) — a separate trust boundary from the in-process LLM (pattern 21) |
| `app/metrics.py`       | Prometheus counters/histograms + the tool-call callback handler |
| `app/eval.py`          | Golden-dataset evaluation harness (`make eval`) |
| `app/chat.py`          | Streaming CLI + Langfuse tracing |
| `app/hitl_demo.py`     | Runnable HITL pause/resume demo against the shared durable graph (`python -m app.hitl_demo "..."`) |
| `app/schemas.py`       | Pydantic request/response models, including `ChatResponse.citations` |
| `app/api.py`           | FastAPI service (`/health`, `/chat`, `/chat/stream`, `/metrics`) |
| `tests/`               | pytest suite — routing/node/graph/tool/checkpointer/sql_store/mcp_server tests against a fake LLM and mocked stores (`make test`, no live services) |

## Troubleshooting

- **`make ingest` fails to embed** → run `make up` and `make pull-models` first.
- **No traces in Langfuse** → make sure you put the keys in `.env` and ran
  `docker compose up -d litellm` afterwards; also restart `make chat`.
- **Model too slow** → `qwen2.5:3b` is small; swap for a bigger
  model in `litellm-config.yaml` if you have the RAM.
