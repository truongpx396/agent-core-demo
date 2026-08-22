"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /             -> the built-in web UI (app/static/index.html)
- GET  /health       -> liveness check
- POST /chat         -> non-streaming, returns full answer (Pydantic in/out)
- POST /chat/stream  -> production SSE streaming via astream_events v2

Reuses the exact same agent runtime as the CLI, so conversation memory
(keyed by thread_id) and Langfuse tracing work identically here.

Run with: `make serve`  (then open http://localhost:8000/docs)

## Identity: trusted headers, NOT authentication (app/security.py)

`get_ctx` reads `X-Tenant-Id`/`X-Principal-Id` and stamps a `SecurityCtx`.
This is the seam a real deployment's auth middleware plugs into — it is
**not** authentication itself: nothing here verifies a password, a JWT, or
a session, and nothing stops a client from sending any header value it
likes. That's fine only because nothing downstream trusts an HTTP request
directly: in production this service sits behind a gateway/reverse proxy
that authenticates the caller and sets these headers itself, stripping any
client-supplied copies first — exactly how `X-Forwarded-*` headers are
conventionally only trusted from a proxy hop, never from the original
client. Wiring that gateway is out of scope here (see GRAPH_PATTERNS.md);
what this app owes is the correct shape at the boundary — required
headers, fail closed (422) if absent, never a body field a client could
set directly, never a default identity — so dropping in real
authentication later is a gateway config change, not a rewrite of this file.
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.agent import answer, astream_events_turn, init_graph_async
from app.schemas import ChatRequest, ChatResponse, HealthResponse
from app.security import SecurityCtx


async def get_ctx(
    x_tenant_id: str = Header(..., description="Trusted-layer tenant id."),
    x_principal_id: str = Header(..., description="Trusted-layer principal id."),
) -> SecurityCtx:
    """FastAPI dependency: required headers, so a request missing either
    one never reaches an endpoint at all (FastAPI returns 422 before the
    handler runs) — the fail-closed behavior lives in the *shape* of the
    dependency, not in a runtime check here."""
    return {"tenant": x_tenant_id, "principal": x_principal_id, "claims": {}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opens the durable checkpointer on uvicorn's own event loop, once, at
    # startup — not lazily on first request. This matters beyond warming
    # the connection: /chat (a plain `def`, so FastAPI runs it in its own
    # threadpool — a different thread than uvicorn's loop) and /chat/stream
    # (`async def`, runs directly on uvicorn's loop) must share ONE
    # checkpointer bound to THIS loop, or the async path hits "bound to a
    # different event loop" — see app/agent.py's module docstring for the
    # full reasoning (verified empirically before writing that doc).
    await init_graph_async()
    yield


app = FastAPI(title="Core AI Stack Demo", version="1.0.0", lifespan=lifespan)

_UI_HTML_PATH = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    """The built-in standalone-product web UI (GRAPH_PATTERNS.md pattern
    29) — a single self-contained page, no build step, no CDN dependency
    (consistent with this app's fully-offline commitment). Talks ONLY to
    `POST /chat/stream`'s published SSE event vocabulary (see that
    endpoint's own docstring) — never a special-cased endpoint of its
    own, so the vocabulary stays the one true contract every client
    (this page, `make chat-stream`, a future one) renders from. Read from
    disk per request (not cached at import time) so editing
    app/static/index.html and refreshing the browser is enough during
    development — this file is tiny and this isn't a hot path.
    """
    return _UI_HTML_PATH.read_text()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/metrics")
def metrics_endpoint() -> Response:
    """Prometheus scrape target — see app/metrics.py for what's recorded."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, ctx: SecurityCtx = Depends(get_ctx)) -> ChatResponse:
    text, citations = answer(req.message, req.thread_id, ctx)
    return ChatResponse(thread_id=req.thread_id, answer=text, citations=citations)


@app.post("/chat/stream")
async def chat_stream(
    req: ChatRequest, ctx: SecurityCtx = Depends(get_ctx)
) -> StreamingResponse:
    """Production SSE endpoint — streams typed events as they happen.

    Each line is a standard Server-Sent Event:
      data: {"type": "token",      "content": "Hello"}
      data: {"type": "tool_start", "tool": "search_docs", "args": {...}}
      data: {"type": "tool_end",   "tool": "search_docs"}
      data: {"type": "citations",  "items": [{"marker": "[1]", ...}]}
      data: {"type": "done"}

    The client can consume this with EventSource or any SSE library.
    Reuse `thread_id` across calls to keep conversation memory.
    """
    async def generate():
        async for event in astream_events_turn(req.message, req.thread_id, ctx):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind a proxy
        },
    )
