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
- POST /billing/webhooks/{provider} -> a payment provider's signed webhook (NOT behind the
                              tenant headers: authenticity is the signature; becomes credits
                              exactly once, see app/api/routers/billing.py)
- GET  /usage             -> this caller's tenant usage/cost, including the
                              rolling-24h number budgets.check_tenant_daily checks
- POST /ingest/upload     -> upload PDF/DOCX documents; each becomes its own
                              job on a SEPARATE queue from chat turns,
                              processed by ingest_worker.py — this endpoint
                              just does MinIO upload + job publish
- GET  /ingest/stream/{job_id} -> SSE progress for one upload's job

Reuses the same agent runtime as the CLI, so memory (by thread_id) and
Langfuse tracing work identically here.

This module is only the app: lifespan, middleware, and the `include_router` calls. The handlers
live in app/api/routers/ (chat.py, ingest.py, usage.py, system.py) and the identity/domain
dependencies in app/api/deps.py. The one thing that must stay HERE is `configure_logging()`
below: uvicorn imports `app.api.main:app` and never runs a `__main__` block.

Run with: `make serve` (then open http://localhost:8000/docs)

## Identity: trusted headers, NOT authentication

See app/api/deps.py (`get_ctx`): the seam a real auth middleware plugs into, not
authentication itself.
"""
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
)
from fastapi.middleware.cors import CORSMiddleware

from app.agent import sql_store
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.api.rate_limit import TenantRateLimitMiddleware
from app.api.routers import billing, chat, ingest, system, usage
from app.core.config import (
    CORS_ALLOWED_ORIGINS,
)
from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry

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
    # Refuses to start (raises) if a payment provider is enabled with no adapter or no signing secret: an
    # endpoint that cannot verify a signature must not come up (app/api/routers/billing.py).
    billing.validate_configuration()
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
app.include_router(chat.router)
app.include_router(usage.router)
app.include_router(billing.router)
app.include_router(ingest.router)
