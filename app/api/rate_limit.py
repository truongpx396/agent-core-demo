"""Per-tenant HTTP rate limiting for app/api/routers/chat.py's turn-creating endpoints —
a single client (or one compromised tenant) must not flood the shared
Redis Streams queue (app/job_queue/queue.py) or starve other tenants' turns.

A plain Starlette middleware over the `limits` library directly (the same
library slowapi wraps), NOT slowapi's `@limiter.limit(...)` decorator —
that requires every decorated endpoint to accept a `request: Request`
param just for the decorator, and this app's tests call handlers directly
as plain functions (tests/api/test_api.py). A middleware sees the raw
request without touching any endpoint signature.

Redis-backed (`limits.storage.RedisStorage`), not in-process memory — a
per-process counter would stop meaning anything once more than one
`uvicorn` process is running (pattern 43). Fails OPEN if Redis is
unreachable, same degrade-don't-crash posture as semantic_cache.py/
moderation.py.
"""
import logging

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import metrics
from app.core.config import (
    BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE,
    RATE_LIMIT_PER_MINUTE,
    REDIS_URL,
)

logger = logging.getLogger(__name__)

# Only endpoints that trigger real LLM/tool work or a heavy parse/embed job —
# never health/metrics/session reads, or POST /chat/cancel (stopping a
# runaway turn must never itself be throttled).
RATE_LIMITED_PATHS = frozenset(
    {"/chat/stream/queued", "/chat/resume", "/ingest/upload"}
)

_storage = storage.storage_from_string(REDIS_URL)
_strategy = strategies.MovingWindowRateLimiter(_storage)
_limit = RateLimitItemPerMinute(RATE_LIMIT_PER_MINUTE)

# The payment-provider webhook has no tenant (the caller is a provider, not a person), so it is limited per SOURCE
# ADDRESS, with its own generous ceiling: a provider redelivering a backlog must not be throttled into a longer
# outage. The signature, not this, is the defence; this only bounds how fast a stranger can make the app compute
# MACs. Behind a proxy every request shares the proxy's address, so this is a global ceiling in practice (disclosed).
WEBHOOK_PATH_PREFIX = "/billing/webhooks/"
_webhook_limit = RateLimitItemPerMinute(BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE)


def _tenant_key(request: Request) -> str:
    """Keyed by TENANT (X-Tenant-Id), not IP — a shared proxy/NAT can put many
    legitimate tenants behind one IP, and tenant is this app's isolation axis
    (app/core/security.py). Falls back to client address if the header is
    missing; that request gets 422'd by get_ctx anyway, this just avoids
    crashing the limiter first."""
    tenant = request.headers.get("x-tenant-id")
    if tenant:
        return tenant
    return request.client.host if request.client else "unknown"


class TenantRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        is_webhook = request.url.path.startswith(WEBHOOK_PATH_PREFIX)
        if request.url.path not in RATE_LIMITED_PATHS and not is_webhook:
            return await call_next(request)

        # A tenant header on the webhook route is the sender's own claim: never a reason to key on it.
        key = f"billing-webhook:{request.client.host if request.client else 'unknown'}" if is_webhook else _tenant_key(request)
        try:
            allowed = _strategy.hit(_webhook_limit if is_webhook else _limit, key)
        except Exception as exc:  # noqa: BLE001 - Redis down must not block the core turn, see module docstring
            logger.warning(
                "rate_limit_check_failed", extra={"error_class": type(exc).__name__}
            )
            allowed = True

        if not allowed:
            metrics.agent_rate_limit_exceeded_total.inc()
            if is_webhook:
                return JSONResponse({"detail": "Too Many Requests"}, status_code=429)
            return JSONResponse(
                {"detail": f"Rate limit exceeded: {RATE_LIMIT_PER_MINUTE} requests per minute per tenant"},
                status_code=429,
            )
        return await call_next(request)
