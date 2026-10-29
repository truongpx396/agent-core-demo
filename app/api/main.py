"""FastAPI service exposing the LangGraph agent over HTTP.

Endpoints:
- GET  /                     built-in web UI (app/api/static/index.html)
- GET  /health               liveness (200 while the process is up)
- GET  /health/ready         readiness (200 only if Qdrant/Postgres/Redis/ml-service are reachable, see
  app/api/health.py; metrics are pushed via OTLP, not pulled here)
- POST /chat/stream/queued   the ONLY way to run a turn over HTTP: the SSE vocabulary an in-process
  astream_events_turn would produce, but the turn runs on a separate agent_worker.py via a Redis Streams queue
  (pattern 43; needs `make agent-worker`). Routes by the `X-Domain` header (default "ecorp", see get_domain)
  to that domain's worker pool; with /chat/resume and /chat/cancel it is where this service serves every
  registered domain
- POST /chat/resume          continues a turn paused at human_approval (HTTP counterpart of
  astream_events_resume), on the same per-domain queue
- POST /chat/cancel          stops a turn, streaming or paused at human_approval
- GET  /chat/sessions        this caller's past threads for the given X-Domain, most recent first (session
  switcher)
- GET  /chat/sessions/{thread_id}/messages            that thread's transcript (404 if it belongs to a
  different domain)
- GET  /chat/sessions/{thread_id}/pending_approval    null, or the tool call(s) still awaiting approval, so
  the web UI can re-show the prompt after a reload or session switch
- POST /billing/webhooks/{provider}   a payment provider's signed webhook (NOT behind the tenant headers:
  authenticity is the signature; becomes credits exactly once, see
  packages/billing/src/credit_billing/http.py)
- GET  /usage                this caller's tenant usage and cost, including the rolling-24h number
  budgets.check_tenant_daily checks
- POST /ingest/upload        upload PDF/DOCX; each file becomes its own job on a SEPARATE queue from chat
  turns, processed by ingest_worker.py (this endpoint only does the MinIO upload and job publish)
- GET  /ingest/stream/{job_id}   SSE progress for one upload's job

It reuses the agent runtime of the CLI, so memory (by thread_id) and Langfuse tracing behave the same here.

This module is only the app: lifespan, middleware and the `include_router` calls. Handlers live in
app/api/routers/ (chat.py, ingest.py, usage.py, system.py), the identity and domain dependencies in
app/api/deps.py. `configure_logging()` below must stay HERE: uvicorn imports `app.api.main:app` and never runs
a `__main__` block.

Run with: `make serve` (then open http://localhost:8000/docs)

## Identity: two modes

See app/api/deps.py (`get_ctx`): `AUTH_MODE=identity` (default) trusts only the signed identity the authlimit
front door adds to a verified request; `AUTH_MODE=headers` trusts `X-Tenant-Id` / `X-Principal-Id` as sent and
is for tests and a developer's own machine. The lifespan refuses to start in identity mode without the front
door's key address, and announces the mode in force.
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
)
from fastapi.middleware.cors import CORSMiddleware

from app.agent.runtime.core import close_checkpointer_pool, init_graph_async
from app.api import identity
from app.api.rate_limit import WebhookRateLimitMiddleware
from app.api.routers import chat, ingest, system, usage
from app.billing_integration import http as billing_http
from app.core import sql_store
from app.core.config import (
    CORS_ALLOWED_ORIGINS,
    settings,
)
from app.core.internal_identity import check_settings
from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry
from app.mcp.connection import close_all_connections

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
    # app/agent/runtime/core.py).
    #
    # configure_telemetry() belongs here, not at module level like
    # configure_logging() above: OTel's set_meter_provider is call-once, so
    # module level would make the first-importing test module under pytest
    # win that race instead. Lifespan never runs under this app's test suite
    # (see tests/api/test_api.py), so this is safe here.
    configure_telemetry("agent-core-api")
    # FIRST, before anything is opened: an API that cannot verify anyone must not come up, and a refusal here leaves no pool to close. Raises
    # `AuthConfigError` naming the variable (AUTHLIMIT_KEYS_URL) when identity mode has no usable address for the front door's keys.
    check_settings(settings)
    identity.warn_if_header_mode(settings.auth_mode)
    # Refuses to start (raises) if a payment provider is enabled with no adapter or no signing secret: an
    # endpoint that cannot verify a signature must not come up (packages/billing/src/credit_billing/http.py).
    billing_http.validate_configuration()
    await init_graph_async()
    # Started last, so a startup that fails above leaves no task behind. The gauge is re-set every minute for as long as the process runs: the
    # collector forgets a series five minutes after its last update.
    announce_task = asyncio.create_task(identity.keep_announcing_auth_mode(settings.auth_mode))
    yield
    announce_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await announce_task
    # Closes the HTTP client that fetches the front door's public keys, on the loop that used it. No-op in header mode (none was made).
    await identity.close()
    # Ends the long-lived connections to MCP servers this process opened (the sandbox bridge, app/mcp/connection.py): each is a
    # child process, and closing here, on the loop that served the requests, terminates it instead of leaving that to the
    # loop's teardown. No-op if no sandbox tool was ever used.
    await close_all_connections()
    # Closes the Postgres pool's background worker threads cleanly
    # (core/sql_store.py, pattern 31) — skipping this leaves them running at exit
    # with a "couldn't stop thread" warning. No-op if the pool was never opened.
    await sql_store.close_pool()
    # Same reasoning for the checkpointer's pool — init_graph_async() above
    # always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


app = FastAPI(title="Core AI Stack Demo", version="1.0.0", lifespan=lifespan)

# A request with no valid identity (401), or one that cannot be checked right now (503), answered in the canonical envelope the front door
# itself uses (app/api/identity.py).
app.add_exception_handler(identity.AuthRefused, identity.auth_refused_handler)

# The payment webhook's per-address limit, the only limit this process enforces itself. How fast a tenant or a person may start turns is the front
# door's job (the authlimit service, spec 018). Plain Starlette middleware, not a per-route decorator, since this app's tests call every handler
# directly as a plain function (see app/api/rate_limit.py).
app.add_middleware(WebhookRateLimitMiddleware)

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
app.include_router(billing_http.router)
app.include_router(ingest.router)
