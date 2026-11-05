"""Per-source-address rate limiting for the payment-provider webhook route, the one limit the API still enforces
itself.

The per-TENANT moving window on turn-creating endpoints (`RATE_LIMIT_PER_MINUTE`, 30) is gone (spec 018): how
fast a tenant or person may start turns is enforced at the front door by the authlimit service, from a tenant
and person it has just verified, as a token bucket in a Redis it shares with no queue. Keeping the old one
would be a second policy point (two numbers to tune, the stricter silently winning, a counter keyed on the
`X-Tenant-Id` HEADER that identity mode doesn't trust). Nothing here limits turns any more, and a test says
so.

What stays, the front door can't cover: the webhook has no tenant and no token (a provider authenticated by
signature) and Caddy lets it through unchecked, so a flood of forged requests is bounded here per source
address before the app computes a MAC for each. It is a plain Starlette middleware over the `limits` library
(what slowapi wraps; slowapi's decorator would make every route take a `request` parameter), Redis-backed so a
second `uvicorn` shares the count (pattern 43), and fails OPEN if Redis is unreachable, like semantic_cache.py
and app/core/moderation.py.
"""
import logging

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import metrics
from app.core.config import BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE, REDIS_URL

logger = logging.getLogger(__name__)

_storage = storage.storage_from_string(REDIS_URL)
_strategy = strategies.MovingWindowRateLimiter(_storage)

# The payment-provider webhook has no tenant (the caller is a provider, not a person), so it is limited per SOURCE
# ADDRESS, with its own generous ceiling: a provider redelivering a backlog must not be throttled into a longer
# outage. The signature, not this, is the defence; this only bounds how fast a stranger can make the app compute
# MACs. Behind a proxy every request shares the proxy's address, so this is a global ceiling in practice (disclosed).
WEBHOOK_PATH_PREFIX = "/billing/webhooks/"
_webhook_limit = RateLimitItemPerMinute(BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE)


class WebhookRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(WEBHOOK_PATH_PREFIX):
            return await call_next(request)

        # A tenant header on the webhook route is the sender's own claim: never a reason to key on it.
        key = f"billing-webhook:{request.client.host if request.client else 'unknown'}"
        try:
            allowed = _strategy.hit(_webhook_limit, key)
        except Exception as exc:  # noqa: BLE001 - Redis down must not block a payment webhook, see module docstring
            logger.warning(
                "rate_limit_check_failed", extra={"error_class": type(exc).__name__}
            )
            allowed = True

        if not allowed:
            metrics.agent_rate_limit_exceeded_total.inc()
            return JSONResponse({"detail": "Too Many Requests"}, status_code=429)
        return await call_next(request)
