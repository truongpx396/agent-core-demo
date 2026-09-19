"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /                  -> built-in web UI (app/api/static/index.html)
- GET  /health            -> liveness (always 200 if the process is up)
- GET  /health/ready      -> readiness (200 only if Qdrant/Postgres/Redis/
                              ml-service are reachable, see app/api/health.py;
                              metrics are pushed via OTLP, not pulled here)
- POST /chat/stream/queued -> the ONLY way to run a turn over HTTP: same SSE
                              event vocabulary an in-process astream_events_turn
                              would produce, but the turn runs on a separate
                              agent_worker.py process via a Redis Streams queue
                              (pattern 43; needs `make agent-worker` running).
                              Routes by `X-Domain` header (default "ecorp",
                              see get_domain) to that domain's worker pool —
                              this and /chat/resume, /chat/cancel are the one
                              place this service serves every registered domain
- POST /chat/resume       -> continues a turn paused at human_approval, the
                              HTTP counterpart to astream_events_resume, same
                              per-domain queue as new turns
- POST /chat/cancel       -> stops a turn, streaming or paused at human_approval
- GET  /chat/sessions     -> this caller's past threads for the given
                              X-Domain, most recent first (session switcher)
- GET  /chat/sessions/{thread_id}/messages -> that thread's transcript
                              (404 if it belongs to a different domain)
- GET  /usage             -> this caller's tenant usage/cost, including the
                              rolling-24h number _tenant_over_daily_budget checks
- POST /ingest/upload     -> upload PDF/DOCX documents; each becomes its own
                              job on a SEPARATE queue from chat turns,
                              processed by ingest_worker.py — this endpoint
                              just does MinIO upload + job publish
- GET  /ingest/stream/{job_id} -> SSE progress for one upload's job

Reuses the same agent runtime as the CLI, so memory (by thread_id) and
Langfuse tracing work identically here.

Run with: `make serve` (then open http://localhost:8000/docs)

## Identity: trusted headers, NOT authentication (app/core/security.py)

`get_ctx` reads `X-Tenant-Id`/`X-Principal-Id` and stamps a `SecurityCtx`.
This is the seam a real auth middleware plugs into, not authentication
itself — nothing verifies a password/JWT/session, and nothing stops a
client sending any header value. That's fine only because production sits
behind a gateway that authenticates the caller and sets these headers
itself, stripping client-supplied copies first (like `X-Forwarded-*`).
What this app owes is the correct shape at the boundary: required headers,
fail closed (422) if absent, never a client-settable body field, never a
default identity — so real auth later is a gateway config change, not a
rewrite here.
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

from app.agent import sessions, sql_store, usage_ledger
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.agent.runtime_stream import get_session_messages
from app.api import (
    health as health_checks,  # `health` is also this module's own liveness endpoint name below
)
from app.api.rate_limit import TenantRateLimitMiddleware
from app.api.schemas import (
    CancelRequest,
    ChatRequest,
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
    MAX_UPLOAD_FILES_PER_REQUEST,
    MAX_UPLOAD_SIZE_MB,
)
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.domains.registry import DOMAINS
from app.ingestion import ingest_queue, object_store
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX
from app.job_queue import queue

# Called at import time, before uvicorn logs anything — without this, the
# service had no logging handler at all, so every `logger.info(...)` call
# was silently discarded under `make serve` (see logging_config.py).
configure_logging()


async def get_ctx(
    x_tenant_id: str = Header(..., description="Trusted-layer tenant id."),
    x_principal_id: str = Header(..., description="Trusted-layer principal id."),
) -> SecurityCtx:
    """Required headers, so a request missing either never reaches an
    endpoint (FastAPI returns 422 before the handler runs) — fail-closed
    lives in the *shape* of the dependency, not a runtime check here."""
    return {"tenant": x_tenant_id, "principal": x_principal_id, "claims": {}}


async def get_domain(
    x_domain: str = Header(
        "ecorp",
        description=(
            "Which domain (app/domains/registry.py) this turn runs against. "
            "Read by every chat endpoint below (all of them queued)."
        ),
    ),
) -> str:
    """Unlike `get_ctx`'s headers, this one defaults rather than fails
    closed — an absent `X-Domain` is the normal case (every caller before
    this existed), so it behaves as before: Ecorp. An UNKNOWN domain is
    still fail-loud (422 here, vs. resolve_domain's startup crash) —
    without this check a typo'd domain would publish onto a requests stream
    no worker pool reads, hanging silently until the caller gives up."""
    if x_domain not in DOMAINS:
        raise HTTPException(
            422, f"Unknown X-Domain {x_domain!r} — must be one of: {', '.join(sorted(DOMAINS))}"
        )
    return x_domain


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opens the durable checkpointer on uvicorn's own event loop, once, at
    # startup, not lazily on first request — GET /chat/sessions/{thread_id}/
    # messages also reads it via get_session_messages, and it must be bound
    # to THIS loop or that call hits "bound to a different event loop" (see
    # app/agent/runtime.py).
    #
    # configure_telemetry() belongs here, not at module level like
    # configure_logging() above: OTel's set_meter_provider is call-once, so
    # module level would make the first-importing test module under pytest
    # win that race instead. Lifespan never runs under this app's test suite
    # (see tests/api/test_api.py), so this is safe here.
    configure_telemetry("agent-core-api")
    await init_graph_async()
    yield
    # Closes the Postgres pool's background worker threads cleanly
    # (sql_store.py, pattern 31) — skipping this leaves them running at exit
    # with a "couldn't stop thread" warning. No-op if the pool was never opened.
    await sql_store.close_pool()
    # Same reasoning for the checkpointer's pool — init_graph_async() above
    # always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


app = FastAPI(title="Core AI Stack Demo", version="1.0.0", lifespan=lifespan)

# Per-tenant, Redis-backed — plain Starlette middleware, not a per-route
# decorator, since this app's tests call every handler directly as a plain
# function (see app/api/rate_limit.py).
app.add_middleware(TenantRateLimitMiddleware)

# "*" (default) is fine for a local demo — the web UI is same-origin and
# never touches CORS. A real multi-origin deployment sets
# CORS_ALLOWED_ORIGINS to a comma-separated allowlist (.env.prod.example).
app.add_middleware(
    CORSMiddleware,
    # nosemgrep: python.fastapi.security.wildcard-cors.wildcard-cors -- gated behind CORS_ALLOWED_ORIGINS, not a hardcoded wildcard; see comment above.
    allow_origins=["*"] if CORS_ALLOWED_ORIGINS == "*" else CORS_ALLOWED_ORIGINS.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

_UI_HTML_PATH = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    """The built-in web UI (pattern 29) — a single self-contained page, no
    build step, no CDN dependency. Talks only to `POST /chat/stream/queued`'s
    SSE vocabulary, never a special-cased endpoint of its own. Read from
    disk per request (not cached) so editing the file and refreshing is
    enough during development — this isn't a hot path.
    """
    return _UI_HTML_PATH.read_text()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness only — always 200 if this process can respond at all. See
    `GET /health/ready` for whether it can actually complete a turn (see
    app/api/health.py on why these stay separate endpoints)."""
    return HealthResponse()


@app.get("/health/ready", response_model=ReadinessResponse)
async def health_ready(response: Response) -> ReadinessResponse:
    """Readiness — 200 only if every real dependency is reachable right now,
    503 otherwise, with which one(s) failed in the body (see
    app/api/health.py::check_dependencies)."""
    checks = await health_checks.check_dependencies()
    ready = all(checks.values())
    response.status_code = 200 if ready else 503
    return ReadinessResponse(status="ready" if ready else "degraded", checks=checks)


def _queued_sse_response(client, request_id: str) -> StreamingResponse:
    """Shared by every queue-backed endpoint below (new turn, resume,
    cancel): relay one job's results stream as SSE frames, cleaning up on a
    terminal event. Factored out since only how the JOB gets published
    differs between the three."""

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
    """Same SSE event vocabulary an in-process `astream_events_turn` would
    produce (token, tool_start, tool_end, citations, followups,
    approval_required, error, done), but this process never runs the graph
    itself — it publishes the turn onto a shared Redis Stream and streams
    back whatever a separate `agent_worker.py` process publishes to this
    turn's results stream (pattern 43, app/job_queue/queue.py).

    This makes the SSE-serving tier and agent-executing tier independently
    scalable: more `uvicorn` processes scale concurrent SSE connections,
    more `agent_worker.py` processes scale concurrent turns. Needs at least
    one worker running (`make agent-worker`) to ever produce a reply.

    A real `approval_required` pause here IS actionable (the worker no
    longer auto-declines it), resumed via `POST /chat/resume` below.

    `domain` (`X-Domain` header, see `get_domain`) picks which domain's
    requests stream this is published onto — only a worker pool booted with
    a matching `AGENT_DOMAIN` reads it, letting one API process serve every
    running domain.
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
    `astream_events_resume`. Always routed through the same Redis queue as a
    new turn (pattern 43), not run in-process: a resume can execute a real
    tool call and run more LLM turns, so handling it in the SSE-serving tier
    would reintroduce the work `POST /chat/stream/queued` exists to move
    off. Any worker in `domain`'s own pool can pick this up — the shared
    checkpoint lives in Postgres, not a worker's process memory. `domain`
    must match the original turn's (the caller is expected to keep sending
    the same `X-Domain` for a given thread_id).

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
    """Stop a turn, whichever of two states it's in — two independent
    mechanisms fire unconditionally (each a no-op if it doesn't apply):

    1. Actively streaming: sets a short-lived Redis flag
       (`queue.py::set_cancel_flag`) the worker running that turn polls
       between graph events (`runtime.py`'s `cancel_check`) — its own
       results stream gets the terminal "cancelled" event directly. Keyed
       by thread_id alone; domain is irrelevant since the worker polls the
       flag directly.
    2. Paused at human_approval: publishes a `"cancel"` job (pattern 36's
       `cancel_run`, over `domain`'s own queue) — picked up by any worker in
       that domain's pool.

    This endpoint's own SSE response is the job-2 outcome specifically —
    not a proxy for job-1's effect on the original turn's stream, which the
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
async def chat_sessions(
    ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> list[SessionSummary]:
    """This caller's own past conversation threads, most recently active
    first — the session switcher's list. Scoped to tenant+principal+domain,
    never tenant alone: a session belongs to whoever started it, same axis
    as Policy.lower's memory scoping (pattern 49 added the domain part), so
    switching domains in the web UI also switches which sessions list."""
    return await sessions.list_sessions(ctx, domain)  # type: ignore[return-value]  # response_model coerces dict -> SessionSummary at the HTTP boundary; a direct Python call (see tests/api/test_api.py) intentionally gets the raw dicts back


@app.get("/chat/sessions/{thread_id}/messages", response_model=list[SessionMessage])
async def chat_session_messages(
    thread_id: str, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> list[SessionMessage]:
    """One session's transcript, for the switcher to replay. `session_belongs_to`
    is checked FIRST and is the entire authorization boundary — the shared
    Postgres checkpointer `get_session_messages` reads has no tenant/
    principal/domain of its own to scope by, so skipping this would let any
    caller read any thread_id's transcript by guessing ids. `domain` must
    match too, extending the same check to a third axis."""
    if not await sessions.session_belongs_to(ctx, thread_id, domain):
        raise HTTPException(status_code=404, detail="session not found")
    return await get_session_messages(thread_id)  # type: ignore[return-value]  # same response_model coercion note as chat_sessions above


@app.get("/usage", response_model=UsageResponse)
async def usage(ctx: SecurityCtx = Depends(get_ctx)) -> UsageResponse:
    """This caller's own tenant usage — exposes the existing
    `usage_summary` over HTTP, so a caller can see how close they are to
    MAX_COST_USD_PER_TENANT_PER_DAY without getting refused first.
    Tenant-scoped only; no way to query another tenant's spend."""
    all_time = await usage_ledger.usage_summary(ctx["tenant"])
    since = datetime.now(UTC) - timedelta(hours=24)
    last_24h = await usage_ledger.usage_summary(ctx["tenant"], since=since)
    return UsageResponse(
        total_tokens=all_time["total_tokens"],
        total_cost_usd=all_time["total_cost_usd"],
        last_24h_cost_usd=last_24h["total_cost_usd"],
        daily_budget_usd=MAX_COST_USD_PER_TENANT_PER_DAY,
    )


_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB per read() call
_MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


async def _read_bounded(upload: UploadFile, filename: str) -> bytes:
    """Reads in chunks and rejects as soon as the running total crosses the
    limit — never materializes much more than one chunk past
    `_MAX_UPLOAD_BYTES`, unlike a bare `await upload.read()` which reads the
    entire file first. An unbounded upload is a memory/storage exhaustion
    vector, not just a slow request."""
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
    """Upload documents to build this tenant's corpus — PDF/DOCX only today
    (app/ingestion/extractors.py); `.txt`/`.md` have their own path
    (ingestor.py::ingest_file, the CLI/script-driven one).

    Fire-and-forget per file: this handler only gets bytes into MinIO and a
    job onto the queue — parsing/embedding happens in ingest_worker.py, on a
    SEPARATE queue from chat turns (see ingest_queue.py). Each file's
    outcome tracks independently via its own `job_id`
    (`GET /ingest/stream/{job_id}`), so one slow/failing file never blocks
    the others.

    An unsupported extension, or too many files, is rejected synchronously
    before any MinIO write. The file-count cap is a UX/abuse guard on this
    one call, distinct from INGEST_WORKER_MAX_CONCURRENCY — it limits how
    many jobs one submission creates, not how many a worker runs at once.
    """
    if len(files) > MAX_UPLOAD_FILES_PER_REQUEST:
        metrics.agent_upload_rejected_total.labels(reason="too_many_files").inc()
        raise HTTPException(
            status_code=400,
            detail=f"{len(files)} files exceeds the {MAX_UPLOAD_FILES_PER_REQUEST}-file "
            "limit per upload — split into multiple submissions",
        )
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
        # SSE-serving process, so a large upload must not stall other requests.
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
    """SSE progress for one upload's job — `{"type": "started"}`, zero or
    more `{"type": "progress", "done": N, "total": M}` while
    ingest_worker.py's embedding loop runs, then one terminal event:
    `{"type": "done", "chunks": N}` or `{"type": "error", ...}`.
    Deliberately NOT ownership-checked against sessions.py-style records
    (this pipeline keeps no job directory) — `job_id` is a `uuid4().hex`,
    unguessable in practice, same posture as the chat results streams.
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
