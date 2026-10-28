"""Per-source-address rate limiting for the payment-provider webhook route, the one limit the API still enforces itself.

This module used to hold a second limiter, a moving window per TENANT on the turn-creating endpoints (`RATE_LIMIT_PER_MINUTE`, 30). That one is gone
(spec 018): the limit on how fast a tenant or a person may start turns is enforced at the front door by the authlimit service, before a request reaches
this process, from a tenant and a person it has just verified, as a token bucket (a burst, then a steady rate) in a Redis it shares with no one's
queue. Keeping the old one would have been a second policy point: two numbers to tune, the stricter silently winning, and a counter keyed on the
`X-Tenant-Id` HEADER, which in identity mode is not an input the API trusts at all. Nothing here limits turns any more, and a test says so.

What stays has a reason the front door cannot cover. The webhook has no tenant and no token (the caller is a payment provider, authenticated by its
signature), and Caddy lets that route through un-checked, so a stranger's flood of forged requests is bounded here, per source address, before the
app computes a MAC for each. It is a plain Starlette middleware over the `limits` library (the one slowapi wraps), not slowapi's decorator, which
would make every route take a `request` parameter just for the decorator; Redis-backed, so a second `uvicorn` process shares the count (pattern 43);
and it fails OPEN if Redis is unreachable, the same degrade-don't-crash posture as semantic_cache.py and app/core/moderation.py.
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
