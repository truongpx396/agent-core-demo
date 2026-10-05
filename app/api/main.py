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
- GET  /chat/sessions/{thread_id}/pending_approval -> null, or the tool
                              call(s) still awaiting approval on this
                              thread — lets the web UI re-show that prompt
                              after a reload/session switch
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

## Identity: trusted headers, NOT authentication

See app/api/deps.py (`get_ctx`): the seam a real auth middleware plugs into, not
authentication itself.
"""
import hashlib
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.agent import sessions, sql_store
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.agent.runtime_stream import get_pending_approval, get_session_messages
from app.api.deps import get_ctx, get_domain
from app.api.rate_limit import TenantRateLimitMiddleware
from app.api.routers import ingest, system, usage
from app.api.schemas import (
    CancelRequest,
    ChatRequest,
    PendingApproval,
    ResumeRequest,
    SessionMessage,
    SessionSummary,
)
from app.core.config import (
    CHAT_FIRST_RESPONSE_DEADLINE_SECONDS,
    CHAT_SUBMIT_DEDUP_TTL_SECONDS,
    CORS_ALLOWED_ORIGINS,
)
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.job_queue import queue

# Called at import time, before uvicorn logs anything — without this, the
# service had no logging handler at all, so every `logger.info(...)` call
# was silently discarded under `make serve` (see logging_config.py).
configure_logging()


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
# CORS_ALLOWED_ORIGINS to a comma-separated allowlist (deploy/env/prod.env.example).
app.add_middleware(
    CORSMiddleware,
    # nosemgrep: python.fastapi.security.wildcard-cors.wildcard-cors -- gated behind CORS_ALLOWED_ORIGINS, not a hardcoded wildcard; see comment above.
    allow_origins=["*"] if CORS_ALLOWED_ORIGINS == "*" else CORS_ALLOWED_ORIGINS.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(usage.router)
app.include_router(ingest.router)

def _queued_sse_response(
    client, request_id: str, *, first_event_deadline_seconds: float = CHAT_FIRST_RESPONSE_DEADLINE_SECONDS
) -> StreamingResponse:
    """Shared by every queue-backed endpoint below (new turn, resume,
    cancel): relay one job's results stream as SSE frames.

    Deliberately does NOT eagerly delete the results stream on a terminal
    event (an earlier version did) — real bug, found via a test exercising
    `POST /chat/stream/queued`'s own submission dedup
    (`claim_or_get_existing_submission`): a dedup HIT means two independent
    callers can legitimately read the exact SAME `request_id`'s results
    stream, one having started well after the other. Eager deletion
    assumed exactly one reader ever existed per `request_id` — true before
    dedup, false now — so whichever reader saw the terminal event FIRST
    deleted the stream out from under the other, which then read an empty
    stream forever (a real client would eventually time out; the hermetic
    test that caught this, using a non-blocking fake Redis, hung outright).
    `RESULTS_STREAM_TTL_SECONDS` already bounds a stream's lifetime
    regardless (`queue.py::publish_result` refreshes it on every write) —
    relying on that alone costs a few minutes of otherwise-idle Redis
    memory per turn, in exchange for correctness under a second reader
    arriving at any point before that TTL elapses.

    `first_event_deadline_seconds` (`queue.py::read_results`'s own param):
    bounds only the wait for the FIRST event ever published to this
    request_id's stream — closes a real hang when nobody is ever going to
    publish anything (no agent-worker running for this domain at all, or
    a submission-dedup claim left pointing at a request_id whose own
    publish then failed — see `chat_stream_queued`'s compensating delete
    below). A legitimately slow-but-alive turn is unaffected: this clears
    the instant the worker's first real event arrives.
    """

    async def generate():
        async for event in queue.read_results(
            client, request_id, first_event_deadline_seconds=first_event_deadline_seconds
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


async def _require_conversation_owner(
    ctx: SecurityCtx, thread_id: str, domain: str, *, claim_with: str | None = None
) -> None:
    """404 unless `ctx` owns `thread_id` under `domain` — the same refusal the
    two GET endpoints give, so a caller can't tell "someone else's" from
    "doesn't exist".

    A conversation's state (checkpoint, cancel flag, thread lock, submission-
    dedup key) is keyed by `thread_id` alone and the id is client-supplied, so
    this check is the whole authorization boundary for send, resume and cancel
    — the worker does not repeat it. It runs BEFORE anything is enqueued or
    any Redis key is written: `/chat/cancel` writes the cancel flag itself, and
    the submission-dedup claim is keyed without a caller, so a check placed
    after either would already have leaked or acted.

    `claim_with` is set only by a send, which may create a new conversation:
    the first claimant of an id owns it (the message becomes the session
    title). Resume and cancel only ever act on an existing one, so they
    verify without claiming. A `telegram:`-prefixed id is never claimable over
    HTTP — see `sessions.TELEGRAM_THREAD_PREFIX`.
    """
    if claim_with is not None and not thread_id.startswith(sessions.TELEGRAM_THREAD_PREFIX):
        allowed = await sessions.claim_session(ctx, thread_id, claim_with, domain)
    else:
        allowed = await sessions.session_belongs_to(ctx, thread_id, domain)
    if not allowed:
        raise HTTPException(status_code=404, detail="session not found")


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

    Deduplicated against an identical (`thread_id`, `message`, `images`)
    resubmission within `CHAT_SUBMIT_DEDUP_TTL_SECONDS`
    (`queue.py::claim_or_get_existing_submission`) — a client's own retry
    of this exact call reuses the first attempt's `request_id` and SSE
    stream rather than publishing (and running) a second, independent
    turn. See that function's own docstring for why the thread lock alone
    doesn't already cover this.
    """
    await _require_conversation_owner(ctx, req.thread_id, domain, claim_with=req.message)
    client = queue.get_client()
    digest = hashlib.sha256(
        json.dumps([req.message, req.images or []]).encode()
    ).hexdigest()
    candidate_request_id = uuid.uuid4().hex
    request_id, is_new = await queue.claim_or_get_existing_submission(
        client,
        thread_id=req.thread_id,
        digest=digest,
        request_id=candidate_request_id,
        ttl_seconds=CHAT_SUBMIT_DEDUP_TTL_SECONDS,
    )
    if is_new:
        try:
            await queue.publish_request(
                client,
                request_id=request_id,
                text=req.message,
                thread_id=req.thread_id,
                ctx=ctx,
                domain=domain,
                images=req.images or None,
            )
        except Exception:
            # The claim above already landed — left as-is, it points at a
            # request_id no job was ever published under, so any retry
            # within CHAT_SUBMIT_DEDUP_TTL_SECONDS would get is_new=False
            # and stream a results stream nobody will ever write to.
            # Release it so a retry claims fresh instead.
            await queue.release_submission_claim(client, thread_id=req.thread_id, digest=digest)
            raise
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
    `astream_events_resume`'s own docstring for why. It must belong to the
    conversation's owner: `_require_conversation_owner` is checked first, so
    approving, rejecting or running another caller's paused action is a 404.
    """
    await _require_conversation_owner(ctx, req.thread_id, domain)
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

    1. Actively streaming — a new turn, or the run that follows an approval
       (`"resume"`): sets a short-lived Redis flag
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
    # BEFORE the flag: this endpoint writes the flag straight to Redis, keyed by
    # thread id alone, so without the check anyone could stop anyone's turn.
    await _require_conversation_owner(ctx, req.thread_id, domain)
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


@app.get("/chat/sessions/{thread_id}/pending_approval", response_model=PendingApproval | None)
async def chat_session_pending_approval(
    thread_id: str, ctx: SecurityCtx = Depends(get_ctx), domain: str = Depends(get_domain)
) -> PendingApproval | None:
    """Lets the session switcher re-show the approve/reject UI for a thread
    that's still paused at human_approval, instead of it looking idle —
    `switchToSession` (index.html) calls this alongside .../messages on
    every switch. Same `session_belongs_to` authorization boundary as
    that endpoint, and for the same reason (get_pending_approval itself
    has no tenant/principal/domain of its own to check)."""
    if not await sessions.session_belongs_to(ctx, thread_id, domain):
        raise HTTPException(status_code=404, detail="session not found")
    return await get_pending_approval(thread_id)  # type: ignore[return-value]  # same response_model coercion note as chat_sessions above
