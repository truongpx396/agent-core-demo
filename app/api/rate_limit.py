"""Per-tenant HTTP rate limiting for app/api/main.py's turn-creating endpoints —
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
from app.core.config import RATE_LIMIT_PER_MINUTE, REDIS_URL

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
        if request.url.path not in RATE_LIMITED_PATHS:
            return await call_next(request)

        key = _tenant_key(request)
        try:
            allowed = _strategy.hit(_limit, key)
        except Exception as exc:  # noqa: BLE001 - Redis down must not block the core turn, see module docstring
            logger.warning(
                "rate_limit_check_failed", extra={"error_class": type(exc).__name__}
            )
            allowed = True

        if not allowed:
            metrics.agent_rate_limit_exceeded_total.inc()
            return JSONResponse(
                {"detail": f"Rate limit exceeded: {RATE_LIMIT_PER_MINUTE} requests per minute per tenant"},
                status_code=429,
            )
        return await call_next(request)
