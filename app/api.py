"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /                  -> the built-in web UI (app/static/index.html)
- GET  /health            -> liveness check
- POST /chat              -> non-streaming, returns full answer (Pydantic in/out)
- POST /chat/stream       -> production SSE streaming via astream_events v2,
                              running the graph in-process, in this request —
                              kept as a documented dev/debug fallback; the
                              web UI's default is /chat/stream/queued below
- POST /chat/stream/queued -> the DEFAULT path: same SSE event vocabulary,
                              but the turn runs on a separate
                              app/agent_worker.py process via a Redis
                              Streams queue (GRAPH_PATTERNS.md pattern 43)
                              — needs `make agent-worker` running, and now
                              a real approve/reject UI can act on a pause
                              it emits (see POST /chat/resume)
- POST /chat/resume       -> continues a turn paused at human_approval —
                              the HTTP counterpart to
                              app/agent.py::astream_events_resume, always
                              routed through the same queue as new turns
- POST /chat/cancel       -> stops a turn, whether it's actively streaming
                              or paused at human_approval
- GET  /chat/sessions     -> this caller's past conversation threads, most
                              recently active first (the session switcher)
- GET  /chat/sessions/{thread_id}/messages -> that thread's transcript

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

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import uuid

from app import queue, sessions, sql_store
from app.agent import answer, astream_events_turn, get_session_messages, init_graph_async
from app.schemas import (
    CancelRequest,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    ResumeRequest,
    SessionMessage,
    SessionSummary,
)
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
    # Closes the Postgres connection pool's background worker threads
    # cleanly (app/sql_store.py, GRAPH_PATTERNS.md pattern 31) — verified
    # empirically that skipping this leaves them still running at process
    # exit with a "couldn't stop thread... within 5.0 seconds" warning.
    # A no-op if nothing in this process ever queried query_employees or
    # wrote to the usage ledger (the pool was never opened).
    sql_store.close_pool()


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
    text, citations, error, ungrounded_claims_count = answer(
        req.message, req.thread_id, ctx, images=req.images or None
    )
    return ChatResponse(
        thread_id=req.thread_id,
        answer=text,
        citations=citations,
        error=error.to_dict() if error else None,
        ungrounded_claims_count=ungrounded_claims_count,
    )


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
      data: {"type": "followups",  "items": ["...", ...]}
      data: {"type": "done"}

    The client can consume this with EventSource or any SSE library.
    Reuse `thread_id` across calls to keep conversation memory.
    """
    async def generate():
        async for event in astream_events_turn(
            req.message, req.thread_id, ctx, images=req.images or None
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind a proxy
        },
    )


def _queued_sse_response(client, request_id: str) -> StreamingResponse:
    """Shared by every queue-backed endpoint below (new turn, resume,
    cancel): relay one job's results stream as SSE frames, cleaning up the
    stream once a terminal event ends the read. Factored out because all
    three endpoints are otherwise identical here — only how the JOB gets
    published differs between them."""

    async def generate():
        try:
            async for event in queue.read_results(client, request_id):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await queue.delete_results_stream(client, request_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind a proxy
        },
    )


@app.post("/chat/stream/queued")
async def chat_stream_queued(
    req: ChatRequest, ctx: SecurityCtx = Depends(get_ctx)
) -> StreamingResponse:
    """Same published SSE event vocabulary as `POST /chat/stream` (see its
    docstring) — but this process never runs the graph itself. It publishes
    the turn as a request onto a shared Redis Stream and streams back
    whatever a separate `app/agent_worker.py` process publishes to this
    turn's own results stream (GRAPH_PATTERNS.md pattern 43, app/queue.py).

    This is what makes the SSE-serving tier and the agent-executing tier
    independently scalable: this endpoint does no LLM/tool work of its own,
    so running more `uvicorn` processes scales concurrent SSE connections,
    and running more `app/agent_worker.py` processes scales concurrent
    turns — neither number constrains the other. Needs at least one
    `app/agent_worker.py` process running (`make agent-worker`) to ever
    produce a reply; with none running, a request just waits on the queue
    until one is (or the client gives up and disconnects).

    Now a real `approval_required` pause from this endpoint IS actionable
    — the worker no longer auto-declines it (see app/agent_worker.py's
    module docstring), so it reaches the caller exactly like
    `POST /chat/stream`'s does, resumed via `POST /chat/resume` below.
    """
    client = queue.get_client()
    request_id = uuid.uuid4().hex
    await queue.publish_request(
        client,
        request_id=request_id,
        text=req.message,
        thread_id=req.thread_id,
        ctx=ctx,
        images=req.images or None,
    )
    return _queued_sse_response(client, request_id)


@app.post("/chat/resume")
async def chat_resume(
    req: ResumeRequest, ctx: SecurityCtx = Depends(get_ctx)
) -> StreamingResponse:
    """Continue a turn paused at human_approval — the HTTP counterpart to
    `app/agent.py::astream_events_resume`. Always routed through the same
    Redis queue as a new turn (GRAPH_PATTERNS.md pattern 43), not run
    in-process: a resume can still execute a real tool call and run many
    more LLM turns, so handling it directly in the SSE-serving tier would
    quietly reintroduce the real graph work `POST /chat/stream/queued`
    exists to move off of it. Any worker can pick this up regardless of
    which one ran (or is even still running) the original turn — the
    checkpoint they all share lives in Postgres, not in a worker's own
    process memory.

    `ctx` is re-supplied here, not reused from the original pause — see
    `astream_events_resume`'s own docstring for why.
    """
    client = queue.get_client()
    request_id = uuid.uuid4().hex
    await queue.publish_resume_request(
        client, request_id=request_id, thread_id=req.thread_id, approved=req.approved, ctx=ctx
    )
    return _queued_sse_response(client, request_id)


@app.post("/chat/cancel")
async def chat_cancel(
    req: CancelRequest, ctx: SecurityCtx = Depends(get_ctx)
) -> StreamingResponse:
    """Stop a turn — whichever of two states it's actually in, handled by
    two independent mechanisms that both fire unconditionally (each is a
    harmless no-op if it doesn't apply):

    1. Actively streaming, not yet paused: sets a short-lived Redis flag
       (`app/queue.py::set_cancel_flag`) the worker CURRENTLY running that
       turn polls between graph events (app/agent.py's `cancel_check`) and
       stops on — its own already-open results stream (the one
       `POST /chat/stream/queued` is reading) gets the terminal
       "cancelled" event directly; nothing more to do here for that case.
    2. Paused at human_approval: publishes a `"cancel"` job
       (GRAPH_PATTERNS.md pattern 36's `cancel_run`, over the queue like
       every other job kind) — picked up by any available worker, since
       nothing is actively running for this thread to signal via the flag.

    This endpoint's OWN SSE response is the job-2 outcome specifically
    (cancelled a real pause, or confirmed nothing was paused here) — not
    a proxy for job-1's effect on the original turn's stream, which the
    caller is expected to already be reading independently.
    """
    client = queue.get_client()
    await queue.set_cancel_flag(client, req.thread_id)
    request_id = uuid.uuid4().hex
    await queue.publish_cancel_request(
        client, request_id=request_id, thread_id=req.thread_id, ctx=ctx
    )
    return _queued_sse_response(client, request_id)


@app.get("/chat/sessions", response_model=list[SessionSummary])
def chat_sessions(ctx: SecurityCtx = Depends(get_ctx)) -> list[SessionSummary]:
    """This caller's own past conversation threads (app/sessions.py),
    most recently active first — the session switcher's list. Scoped to
    tenant+principal, never tenant alone: a session belongs to whoever
    started it (own-conversation isolation), the same axis
    app/security.py::Policy.lower already applies to memories."""
    return sessions.list_sessions(ctx)


@app.get("/chat/sessions/{thread_id}/messages", response_model=list[SessionMessage])
async def chat_session_messages(
    thread_id: str, ctx: SecurityCtx = Depends(get_ctx)
) -> list[SessionMessage]:
    """One session's transcript, for the switcher to replay when a user
    picks it. `session_belongs_to` is checked FIRST and is the entire
    authorization boundary here — the shared Postgres checkpointer
    `get_session_messages` reads has no tenant/principal of its own to
    scope by, so skipping this check would let any caller read any
    thread_id's transcript just by guessing/enumerating ids."""
    if not sessions.session_belongs_to(ctx, thread_id):
        raise HTTPException(status_code=404, detail="session not found")
    return await get_session_messages(thread_id)
