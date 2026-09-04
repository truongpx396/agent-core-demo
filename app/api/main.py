"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /                  -> the built-in web UI (app/api/static/index.html)
- GET  /health            -> liveness check (always 200 if the process is up)
- GET  /health/ready      -> readiness check (200 only if Qdrant/Postgres/
                              Redis are actually reachable, see app/api/health.py)
                              (metrics are pushed via OTLP, not pulled from an
                              endpoint here — see app/core/telemetry.py and
                              docker-compose.observability.yml)
- POST /chat              -> non-streaming, returns full answer (Pydantic in/out)
- POST /chat/stream       -> production SSE streaming via astream_events v2,
                              running the graph in-process, in this request —
                              kept as a documented dev/debug fallback; the
                              web UI's default is /chat/stream/queued below
- POST /chat/stream/queued -> the DEFAULT path: same SSE event vocabulary,
                              but the turn runs on a separate
                              app/turns/agent_worker.py process via a Redis
                              Streams queue (GRAPH_PATTERNS.md pattern 43)
                              — needs `make agent-worker` running, and now
                              a real approve/reject UI can act on a pause
                              it emits (see POST /chat/resume). Routes by
                              an `X-Domain` header (default "acme", see
                              get_domain) to that domain's own worker pool
                              — this endpoint (and /chat/resume,
                              /chat/cancel below) is the ONE place this
                              service serves every domain
                              app/domains/registry.py knows about from a
                              single unified process; POST /chat and
                              POST /chat/stream above stay Acme-only
- POST /chat/resume       -> continues a turn paused at human_approval —
                              the HTTP counterpart to
                              app/agent/runtime.py::astream_events_resume, always
                              routed through the same per-domain queue as
                              new turns (same X-Domain header)
- POST /chat/cancel       -> stops a turn, whether it's actively streaming
                              or paused at human_approval (same X-Domain
                              header for the paused-job case)
- GET  /chat/sessions     -> this caller's past conversation threads for
                              the given X-Domain, most recently active
                              first (the session switcher)
- GET  /chat/sessions/{thread_id}/messages -> that thread's transcript
                              (404 if it belongs to a different domain)
- GET  /usage             -> this caller's own tenant usage/cost (app/agent/meter.py),
                              including the rolling-24h number
                              app/agent/runtime.py::_tenant_over_daily_budget checks
- POST /ingest/upload     -> upload one or more PDF/DOCX documents; each
                              becomes its own job on a SEPARATE queue from
                              chat turns (app/ingestion/ingest_queue.py), processed
                              by app/ingestion/ingest_worker.py — this endpoint does
                              no parsing/embedding of its own, just
                              MinIO upload + job publish
- GET  /ingest/stream/{job_id} -> SSE progress for one upload's job

Reuses the exact same agent runtime as the CLI, so conversation memory
(keyed by thread_id) and Langfuse tracing work identically here.

Run with: `make serve`  (then open http://localhost:8000/docs)

## Identity: trusted headers, NOT authentication (app/core/security.py)

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
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

from app.agent import meter, sessions, sql_store
from app.agent.runtime import (
    answer,
    astream_events_turn,
    close_checkpointer_pool,
    get_session_messages,
    init_graph_async,
)
from app.api import (
    health as health_checks,  # `health` is also this module's own liveness endpoint name below
)
from app.api.rate_limit import TenantRateLimitMiddleware
from app.api.schemas import (
    CancelRequest,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    IngestUploadResult,
    ReadinessResponse,
    ResumeRequest,
    SessionMessage,
    SessionSummary,
    UsageResponse,
)
from app.core import metrics
from app.core.config import (
    CORS_ALLOWED_ORIGINS,
    MAX_COST_USD_PER_TENANT_PER_DAY,
    MAX_UPLOAD_SIZE_MB,
)
from app.core.logging_config import bind_request_id, configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.domains.registry import DOMAINS
from app.ingestion import ingest_queue, object_store
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX
from app.turns import queue

# Called at import time, before uvicorn (or anything else) has a chance to
# log through this module — see app/core/logging_config.py's own docstring for
# why this matters here specifically: without it, this service had NO
# logging handler configured at all (verified empirically), so every
# `logger.info(...)` call throughout this app's tool/graph/turn
# instrumentation was silently discarded under `make serve`, not merely
# unstructured.
configure_logging()


async def get_ctx(
    x_tenant_id: str = Header(..., description="Trusted-layer tenant id."),
    x_principal_id: str = Header(..., description="Trusted-layer principal id."),
) -> SecurityCtx:
    """FastAPI dependency: required headers, so a request missing either
    one never reaches an endpoint at all (FastAPI returns 422 before the
    handler runs) — the fail-closed behavior lives in the *shape* of the
    dependency, not in a runtime check here."""
    return {"tenant": x_tenant_id, "principal": x_principal_id, "claims": {}}


async def get_domain(
    x_domain: str = Header(
        "acme",
        description=(
            "Which domain (app/domains/registry.py) this turn runs against. "
            "Only the QUEUED chat endpoints read this — POST /chat and "
            "POST /chat/stream stay hardcoded to Acme, unaffected by it."
        ),
    ),
) -> str:
    """Unlike `get_ctx`'s two headers, this one defaults rather than fails
    closed — an absent `X-Domain` is a normal, common case (every existing
    caller before this dependency existed), not a misconfiguration, so it
    should behave exactly as before: Acme. An UNKNOWN domain name is still
    fail-loud, though — same discipline as
    app/domains/registry.py::resolve_domain itself, just surfaced as a 422
    here instead of a process-startup crash, since this is a per-request
    value rather than a per-process one. Without this check, a typo'd
    domain would publish onto a requests stream no running
    app/turns/agent_worker.py pool ever reads, hanging silently until the
    caller gives up — a much worse failure mode than an immediate 422."""
    if x_domain not in DOMAINS:
        raise HTTPException(
            422, f"Unknown X-Domain {x_domain!r} — must be one of: {', '.join(sorted(DOMAINS))}"
        )
    return x_domain


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opens the durable checkpointer on uvicorn's own event loop, once, at
    # startup — not lazily on first request. This matters beyond warming
    # the connection: /chat (a plain `def`, so FastAPI runs it in its own
    # threadpool — a different thread than uvicorn's loop) and /chat/stream
    # (`async def`, runs directly on uvicorn's loop) must share ONE
    # checkpointer bound to THIS loop, or the async path hits "bound to a
    # different event loop" — see app/agent/runtime.py's module docstring for the
    # full reasoning (verified empirically before writing that doc).
    #
    # configure_telemetry() belongs here, not at module level like
    # configure_logging() above: it installs a real, network-bound OTLP
    # MeterProvider, and OTel's set_meter_provider is call-once — the FIRST
    # caller in the process wins, permanently. Module level would make that
    # first caller whichever test module happens to import this file first
    # under pytest (see app/core/telemetry.py's own docstring). Lifespan
    # never runs under this app's test suite (no test drives this app
    # through TestClient as a context manager — see tests/api/test_api.py's
    # docstring), so this is safe here.
    configure_telemetry("agent-core-api")
    await init_graph_async()
    yield
    # Closes the Postgres connection pool's background worker threads
    # cleanly (app/agent/sql_store.py, GRAPH_PATTERNS.md pattern 31) — verified
    # empirically that skipping this leaves them still running at process
    # exit with a "couldn't stop thread... within 5.0 seconds" warning.
    # A no-op if nothing in this process ever queried query_employees or
    # wrote to the usage ledger (the pool was never opened).
    sql_store.close_pool()
    # Same reasoning for the checkpointer's own pool (app/agent/runtime.py) —
    # init_graph_async() above always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


app = FastAPI(title="Core AI Stack Demo", version="1.0.0", lifespan=lifespan)

# Per-tenant, Redis-backed — see app/api/rate_limit.py's own module docstring
# for why this is a plain Starlette middleware rather than a per-route
# decorator (this app's test suite calls every handler directly as a
# plain function, and a decorator needing its own `request: Request`
# parameter on every rate-limited endpoint would mean changing all of
# their signatures just to satisfy it).
app.add_middleware(TenantRateLimitMiddleware)

# "*" (the default, CORS_ALLOWED_ORIGINS) is fine for a local demo with no
# other frontend pointed at this API — app/api/static/index.html is
# same-origin and never touches CORS at all. A real multi-origin
# deployment sets CORS_ALLOWED_ORIGINS to a comma-separated allowlist.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if CORS_ALLOWED_ORIGINS == "*" else CORS_ALLOWED_ORIGINS.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    app/api/static/index.html and refreshing the browser is enough during
    development — this file is tiny and this isn't a hot path.
    """
    return _UI_HTML_PATH.read_text()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness only — always 200 if this process can respond at all. See
    `GET /health/ready` for whether it can actually complete a turn right
    now; conflating the two into one endpoint would make an orchestrator
    unable to tell "the process is wedged" apart from "a downstream store
    is down" (app/api/health.py's module docstring)."""
    return HealthResponse()


@app.get("/health/ready", response_model=ReadinessResponse)
async def health_ready(response: Response) -> ReadinessResponse:
    """Readiness — 200 only if every real dependency (Qdrant, both
    Postgres databases, Redis) is actually reachable right now, 503
    otherwise, with which one(s) failed in the body — see
    app/api/health.py::check_dependencies for what "reachable" means for each.
    """
    checks = await health_checks.check_dependencies()
    ready = all(checks.values())
    response.status_code = 200 if ready else 503
    return ReadinessResponse(status="ready" if ready else "degraded", checks=checks)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, ctx: SecurityCtx = Depends(get_ctx)) -> ChatResponse:
    # thread_id doubles as the correlation id here — this path has no
    # separate request_id concept (that's the queued path's own thing, see
    # app/turns/agent_worker.py::process_request) — so every log line touched
    # while answering THIS request, all the way down into app/agent/graph.py and
    # app/agent/tools.py, carries it (app/core/logging_config.py's bind_request_id).
    with bind_request_id(req.thread_id):
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
        # Bound INSIDE the generator, not around this call — `generate()`
        # runs lazily, driven by StreamingResponse iterating it after
        # chat_stream() itself has already returned, so binding out there
        # would cover the wrong span (see app/core/logging_config.py's
        # bind_request_id).
        with bind_request_id(req.thread_id):
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
    req: ChatRequest, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> StreamingResponse:
    """Same published SSE event vocabulary as `POST /chat/stream` (see its
    docstring) — but this process never runs the graph itself. It publishes
    the turn as a request onto a shared Redis Stream and streams back
    whatever a separate `app/turns/agent_worker.py` process publishes to this
    turn's own results stream (GRAPH_PATTERNS.md pattern 43, app/turns/queue.py).

    This is what makes the SSE-serving tier and the agent-executing tier
    independently scalable: this endpoint does no LLM/tool work of its own,
    so running more `uvicorn` processes scales concurrent SSE connections,
    and running more `app/turns/agent_worker.py` processes scales concurrent
    turns — neither number constrains the other. Needs at least one
    `app/turns/agent_worker.py` process running (`make agent-worker`) to ever
    produce a reply; with none running, a request just waits on the queue
    until one is (or the client gives up and disconnects).

    Now a real `approval_required` pause from this endpoint IS actionable
    — the worker no longer auto-declines it (see app/turns/agent_worker.py's
    module docstring), so it reaches the caller exactly like
    `POST /chat/stream`'s does, resumed via `POST /chat/resume` below.

    `domain` (an `X-Domain` header, see `get_domain`) picks which domain's
    requests stream this turn is published onto — only an
    `app/turns/agent_worker.py` pool booted with a matching `AGENT_DOMAIN`
    ever reads it, so this is what lets ONE unified API process serve every
    domain a worker pool is currently running for.
    """
    client = queue.get_client()
    request_id = uuid.uuid4().hex
    await queue.publish_request(
        client,
        request_id=request_id,
        text=req.message,
        thread_id=req.thread_id,
        ctx=ctx,
        domain=domain,
        images=req.images or None,
    )
    return _queued_sse_response(client, request_id)


@app.post("/chat/resume")
async def chat_resume(
    req: ResumeRequest, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> StreamingResponse:
    """Continue a turn paused at human_approval — the HTTP counterpart to
    `app/agent/runtime.py::astream_events_resume`. Always routed through the same
    Redis queue as a new turn (GRAPH_PATTERNS.md pattern 43), not run
    in-process: a resume can still execute a real tool call and run many
    more LLM turns, so handling it directly in the SSE-serving tier would
    quietly reintroduce the real graph work `POST /chat/stream/queued`
    exists to move off of it. Any worker IN `domain`'s OWN pool can pick
    this up regardless of which one ran (or is even still running) the
    original turn — the checkpoint they all share lives in Postgres, not in
    a worker's own process memory. `domain` must match the ORIGINAL turn's
    (see `queue.publish_request`'s docstring for why) — the caller (the web
    UI) is expected to keep sending the same `X-Domain` for a given
    thread_id throughout its conversation.

    `ctx` is re-supplied here, not reused from the original pause — see
    `astream_events_resume`'s own docstring for why.
    """
    client = queue.get_client()
    request_id = uuid.uuid4().hex
    await queue.publish_resume_request(
        client,
        request_id=request_id,
        thread_id=req.thread_id,
        approved=req.approved,
        ctx=ctx,
        domain=domain,
    )
    return _queued_sse_response(client, request_id)


@app.post("/chat/cancel")
async def chat_cancel(
    req: CancelRequest, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> StreamingResponse:
    """Stop a turn — whichever of two states it's actually in, handled by
    two independent mechanisms that both fire unconditionally (each is a
    harmless no-op if it doesn't apply):

    1. Actively streaming, not yet paused: sets a short-lived Redis flag
       (`app/turns/queue.py::set_cancel_flag`) the worker CURRENTLY running that
       turn polls between graph events (app/agent/runtime.py's `cancel_check`) and
       stops on — its own already-open results stream (the one
       `POST /chat/stream/queued` is reading) gets the terminal
       "cancelled" event directly; nothing more to do here for that case.
       Keyed by thread_id alone, not by domain — whichever worker is
       actually running the turn polls this flag directly, so which
       domain it belongs to is irrelevant here.
    2. Paused at human_approval: publishes a `"cancel"` job
       (GRAPH_PATTERNS.md pattern 36's `cancel_run`, over `domain`'s own
       queue like every other job kind) — picked up by any available
       worker IN THAT DOMAIN's pool, since nothing is actively running for
       this thread to signal via the flag.

    This endpoint's OWN SSE response is the job-2 outcome specifically
    (cancelled a real pause, or confirmed nothing was paused here) — not
    a proxy for job-1's effect on the original turn's stream, which the
    caller is expected to already be reading independently.
    """
    client = queue.get_client()
    await queue.set_cancel_flag(client, req.thread_id)
    request_id = uuid.uuid4().hex
    await queue.publish_cancel_request(
        client, request_id=request_id, thread_id=req.thread_id, ctx=ctx, domain=domain
    )
    return _queued_sse_response(client, request_id)


@app.get("/chat/sessions", response_model=list[SessionSummary])
def chat_sessions(
    ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> list[SessionSummary]:
    """This caller's own past conversation threads (app/agent/sessions.py),
    most recently active first — the session switcher's list. Scoped to
    tenant+principal+domain, never tenant alone: a session belongs to
    whoever started it (own-conversation isolation), the same axis
    app/core/security.py::Policy.lower already applies to memories — and,
    since GRAPH_PATTERNS.md pattern 49, to whichever domain actually ran it
    too, via the same `X-Domain` header the queued chat endpoints read
    (`get_domain`), so switching domains in the web UI also switches which
    sessions the switcher lists."""
    return sessions.list_sessions(ctx, domain)  # type: ignore[return-value]  # response_model coerces dict -> SessionSummary at the HTTP boundary; a direct Python call (see tests/api/test_api.py) intentionally gets the raw dicts back


@app.get("/chat/sessions/{thread_id}/messages", response_model=list[SessionMessage])
async def chat_session_messages(
    thread_id: str, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> list[SessionMessage]:
    """One session's transcript, for the switcher to replay when a user
    picks it. `session_belongs_to` is checked FIRST and is the entire
    authorization boundary here — the shared Postgres checkpointer
    `get_session_messages` reads has no tenant/principal (or domain) of its
    own to scope by, so skipping this check would let any caller read any
    thread_id's transcript just by guessing/enumerating ids. Requiring
    `domain` to match too means a thread opened under a different domain
    404s here exactly like a different tenant's/principal's thread already
    did — not a new failure mode, the same one extended to a third axis."""
    if not sessions.session_belongs_to(ctx, thread_id, domain):
        raise HTTPException(status_code=404, detail="session not found")
    return await get_session_messages(thread_id)  # type: ignore[return-value]  # same response_model coercion note as chat_sessions above


@app.get("/usage", response_model=UsageResponse)
def usage(ctx: SecurityCtx = Depends(get_ctx)) -> UsageResponse:
    """This caller's own tenant usage (app/agent/meter.py) — the read path that
    was already there (`usage_summary`), just not reachable over HTTP
    before now, so a caller had no way to see how close they were to
    MAX_COST_USD_PER_TENANT_PER_DAY short of getting refused by
    app/agent/runtime.py::_tenant_over_daily_budget first. Tenant-scoped only,
    same as `usage_summary` itself — no way to query another tenant's
    spend through this."""
    all_time = meter.usage_summary(ctx["tenant"])
    since = datetime.now(UTC) - timedelta(hours=24)
    last_24h = meter.usage_summary(ctx["tenant"], since=since)
    return UsageResponse(
        total_tokens=all_time["total_tokens"],
        total_cost_usd=all_time["total_cost_usd"],
        last_24h_cost_usd=last_24h["total_cost_usd"],
        daily_budget_usd=MAX_COST_USD_PER_TENANT_PER_DAY,
    )


_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB per read() call
_MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


async def _read_bounded(upload: UploadFile, filename: str) -> bytes:
    """Reads in chunks and rejects as soon as the running total crosses
    the limit — never materializes more than roughly one chunk past
    `_MAX_UPLOAD_BYTES` in memory, unlike a bare `await upload.read()`
    (what this replaced), which reads the ENTIRE file into memory first
    and only then would have anything to check the size of. An unbounded
    upload is a memory/storage exhaustion vector, not just a slow
    request."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            metrics.agent_upload_rejected_total.labels(reason="too_large").inc()
            raise HTTPException(
                status_code=413,
                detail=f"{filename!r} exceeds the {MAX_UPLOAD_SIZE_MB}MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/ingest/upload", response_model=list[IngestUploadResult])
async def ingest_upload(
    files: list[UploadFile] = File(...),
    topic: str | None = Form(None),
    ctx: SecurityCtx = Depends(get_ctx),
) -> list[IngestUploadResult]:
    """Upload one or more documents to build this tenant's retrievable
    corpus — PDF/DOCX only today (app/ingestion/extractors.py); `.txt`/`.md` already
    have their own path (app/ingestion/ingestor.py::ingest_file, the CLI/script-driven
    one).

    Fire-and-forget per file, not a blocking wait: this handler's only
    job is getting the bytes into MinIO and a job onto the queue — actual
    parsing/embedding happens in app/ingestion/ingest_worker.py, a separate process
    on a SEPARATE queue from chat turns (see app/ingestion/ingest_queue.py's module
    docstring for why sharing one would be a mistake). Each file's
    outcome is tracked independently via its own `job_id`
    (`GET /ingest/stream/{job_id}`), so one slow/failing file in a batch
    never blocks or hides the others.

    An unsupported extension is rejected here, synchronously, before any
    MinIO write — no reason to pay for an upload this app already knows
    it can't process.
    """
    client = queue.get_client()  # ingest_queue reuses this same Redis client — see its module docstring
    results = []
    for upload in files:
        filename = Path(upload.filename or "").name  # strip any path component a client might send
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTRACTORS_BY_SUFFIX:
            metrics.agent_upload_rejected_total.labels(reason="bad_file_type").inc()
            raise HTTPException(
                status_code=400,
                detail=f"unsupported file type {suffix!r} for {filename!r} — "
                f"only {sorted(EXTRACTORS_BY_SUFFIX)} are supported",
            )
        data = await _read_bounded(upload, filename)
        job_id = uuid.uuid4().hex
        object_key = f"{ctx['tenant']}/{job_id}-{filename}"
        # Blocking MinIO I/O off the event loop — this IS the shared
        # SSE-serving process (unlike app/ingestion/ingest_worker.py, a dedicated
        # single-purpose process where the same call runs directly), so a
        # large upload must not stall every other concurrent request.
        await asyncio.to_thread(
            object_store.upload_bytes,
            object_key,
            data,
            upload.content_type or "application/octet-stream",
        )
        await ingest_queue.publish_ingest_request(
            client,
            job_id=job_id,
            object_key=object_key,
            filename=filename,
            content_type=upload.content_type or "application/octet-stream",
            ctx=ctx,
            topic=topic,
        )
        results.append(IngestUploadResult(filename=filename, job_id=job_id))
    return results


@app.get("/ingest/stream/{job_id}")
async def ingest_stream(job_id: str) -> StreamingResponse:
    """SSE progress for one upload's job — `{"type": "started"}`, then
    exactly one terminal event: `{"type": "done", "chunks": N}` or
    `{"type": "error", "content": "..."}`. Deliberately NOT
    ownership-checked against app/agent/sessions.py-style records (this
    pipeline doesn't maintain a job directory the way chat sessions do) —
    `job_id` is a `uuid4().hex`, unguessable in practice, the same
    "unguessable id is the access control" posture
    app/turns/queue.py's chat results streams already rely on.
    """
    client = ingest_queue.get_client()

    async def generate():
        try:
            async for event in ingest_queue.read_results(client, job_id):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await ingest_queue.delete_results_stream(client, job_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
