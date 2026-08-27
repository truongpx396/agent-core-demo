"""Per-tenant HTTP rate limiting for app/api.py's turn-creating endpoints —
a single client (or one misbehaving/compromised tenant) must not be able
to flood the shared Redis Streams queue (app/queue.py) or starve every
other tenant's turns.

A plain Starlette middleware over the `limits` library's engine directly
(the same library slowapi wraps) — NOT slowapi's own `@limiter.limit(...)`
route decorator, which requires every decorated endpoint to accept a
`request: Request` parameter purely for the decorator's own use. This
app's own test suite calls every app/api.py handler directly as a plain
Python function (see tests/test_api.py's module docstring) rather than
through a real ASGI request — adding a required, test-irrelevant `Request`
parameter to every rate-limited endpoint's signature would mean updating
every existing direct-call test to construct one just to satisfy the
decorator, for a concern (rate limiting) those tests have nothing to do
with. A middleware sees the raw request already, without touching any
endpoint's signature at all.

Redis-backed (`limits.storage.RedisStorage`), not in-process memory — a
per-process counter would silently stop meaning anything the moment more
than one `uvicorn` process is running (this app's own horizontal-scaling
story, see GRAPH_PATTERNS.md pattern 43), since each process would count
hits independently instead of sharing one real count across all of them.
Fails OPEN if Redis itself is unreachable — the same "an ancillary
system's outage must not take down the core turn" posture
app/semantic_cache.py and app/moderation.py already established
(degrade-don't-crash), applied here to a third ancillary system rather
than silently becoming the one exception to it.
"""
import logging

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import metrics
from app.config import RATE_LIMIT_PER_MINUTE, REDIS_URL

logger = logging.getLogger(__name__)

# Only the endpoints that can actually trigger real LLM/tool work or a
# heavy parse/embed job — never GET /health(/ready), GET /metrics, the
# session-listing reads, or POST /chat/cancel (a client trying to STOP a
# runaway turn must never itself be throttled).
RATE_LIMITED_PATHS = frozenset(
    {"/chat", "/chat/stream", "/chat/stream/queued", "/chat/resume", "/ingest/upload"}
)

_storage = storage.storage_from_string(REDIS_URL)
_strategy = strategies.MovingWindowRateLimiter(_storage)
_limit = RateLimitItemPerMinute(RATE_LIMIT_PER_MINUTE)


def _tenant_key(request: Request) -> str:
    """Keyed by TENANT (X-Tenant-Id), not by IP — a shared reverse
    proxy/NAT can put many distinct, legitimate tenants behind one IP, and
    the isolation axis this whole app is built around (app/security.py) is
    tenant, not network address. A request missing the header falls back
    to the client address — it's about to get rejected with 422 by
    get_ctx's own dependency anyway; this is just about not crashing the
    limiter itself on the way there."""
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
