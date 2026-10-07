"""Tests for app/api/rate_limit.py's per-tenant HTTP rate limiting.

An in-memory `limits` storage backend (`memory://`) stands in for Redis —
hermetic, no live service needed, same "fake the collaborator, not the
network" approach the rest of this suite uses. The middleware's
`dispatch()` is called directly (via asyncio.run — no pytest-asyncio
plugin here, see tests/job_queue/test_queue.py's own module docstring) with a
hand-built Request, the same "test the function, not the framework
wiring" approach tests/api/test_api.py's module docstring already establishes
for this codebase's route handlers.
"""

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.requests import Request
from starlette.responses import Response

from app.api import rate_limit
from app.core import metrics
from tests.conftest import metric_value


def _make_request(path: str, *, tenant: str | None = "ecorp", client_host="10.0.0.1") -> Request:
    headers = [(b"x-tenant-id", tenant.encode())] if tenant else []
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
        "client": (client_host, 5555),
    }
    return Request(scope)


async def _call_next(request: Request) -> Response:
    return Response("ok", status_code=200)


def _fresh_middleware(monkeypatch, *, limit_per_minute: int):
    """A middleware instance backed by its own isolated in-memory store
    (not the real module-level singleton, which would leak counts between
    tests) — same limit every real request in a test hits against."""
    mem_storage = storage.storage_from_string("memory://")
    strategy = strategies.MovingWindowRateLimiter(mem_storage)
    monkeypatch.setattr(rate_limit, "_strategy", strategy)
    monkeypatch.setattr(rate_limit, "_limit", RateLimitItemPerMinute(limit_per_minute))
    return rate_limit.TenantRateLimitMiddleware(app=None)


class TestTenantRateLimitMiddleware:
    async def test_unrated_path_is_never_limited(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/health")
        # Three "hits" against a 1/minute limit would fail if this path
        # were rate-limited at all — GET /health must never be.
        for _ in range(3):
            response = await middleware.dispatch(request, _call_next)
            assert response.status_code == 200

    async def test_allows_up_to_the_limit_then_rejects_with_429(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=2)
        request = _make_request("/chat/stream/queued")

        first = await middleware.dispatch(request, _call_next)
        second = await middleware.dispatch(request, _call_next)
        third = await middleware.dispatch(request, _call_next)

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429

    async def test_different_tenants_get_independent_budgets(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        ecorp_request = _make_request("/chat/stream/queued", tenant="ecorp")
        other_request = _make_request("/chat/stream/queued", tenant="other-tenant")

        assert (await middleware.dispatch(ecorp_request, _call_next)).status_code == 200
        # ecorp is now over budget — a DIFFERENT tenant hitting the same
        # path must not be affected by ecorp's own count.
        assert (await middleware.dispatch(ecorp_request, _call_next)).status_code == 429
        assert (await middleware.dispatch(other_request, _call_next)).status_code == 200

    async def test_missing_tenant_header_falls_back_to_client_address(self, monkeypatch):
        """Doesn't crash the limiter — the request is about to be rejected
        with 422 by get_ctx's own dependency anyway once it actually
        reaches a real endpoint."""
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/chat/stream/queued", tenant=None, client_host="10.0.0.9")
        response = await middleware.dispatch(request, _call_next)
        assert response.status_code == 200

    async def test_storage_failure_fails_open_not_closed(self, monkeypatch):
        """Redis (or here, the storage backend) being unreachable must
        never block the core turn — same degrade-don't-crash posture as
        app/retrieval/semantic_cache.py and app/agent/moderation.py."""
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)

        def _broken_hit(*args, **kwargs):
            raise ConnectionError("storage unreachable")

        monkeypatch.setattr(rate_limit._strategy, "hit", _broken_hit)
        request = _make_request("/chat/stream/queued")

        response = await middleware.dispatch(request, _call_next)
        assert response.status_code == 200

    async def test_rejection_increments_the_metric(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/chat/stream/queued")
        before = metric_value(metrics.agent_rate_limit_exceeded_total)

        await middleware.dispatch(request, _call_next)  # consumes the budget
        await middleware.dispatch(request, _call_next)  # rejected

        after = metric_value(metrics.agent_rate_limit_exceeded_total)
        assert after == before + 1


class TestTheWebhookRoute:
    """POST /billing/webhooks/{provider} has no tenant (the caller is a payment provider), so it is limited per
    SOURCE ADDRESS with its own ceiling (app/api/rate_limit.py)."""

    @staticmethod
    def _middleware(monkeypatch, *, webhook_limit: int, tenant_limit: int = 100):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=tenant_limit)
        monkeypatch.setattr(rate_limit, "_webhook_limit", RateLimitItemPerMinute(webhook_limit))
        return middleware

    async def test_it_is_limited_per_source_address(self, monkeypatch):
        middleware = self._middleware(monkeypatch, webhook_limit=2)
        provider_ip = _make_request("/billing/webhooks/fake", tenant=None, client_host="203.0.113.7")
        other_ip = _make_request("/billing/webhooks/fake", tenant=None, client_host="198.51.100.9")

        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 200
        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 200
        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 429
        assert (await middleware.dispatch(other_ip, _call_next)).status_code == 200, "one source's flood is not another's"

    async def test_a_tenant_header_on_the_webhook_route_is_never_a_reason_to_key_on_it(self, monkeypatch):
        """The header is the sender's own claim: rotating it must not buy a fresh budget."""
        middleware = self._middleware(monkeypatch, webhook_limit=1)

        first = _make_request("/billing/webhooks/fake", tenant="a", client_host="203.0.113.7")
        second = _make_request("/billing/webhooks/fake", tenant="b", client_host="203.0.113.7")

        assert (await middleware.dispatch(first, _call_next)).status_code == 200
        assert (await middleware.dispatch(second, _call_next)).status_code == 429

    async def test_it_has_its_own_ceiling_apart_from_the_per_tenant_one(self, monkeypatch):
        """A provider redelivering a backlog must not be throttled by the (much lower) per-tenant chat limit."""
        middleware = self._middleware(monkeypatch, webhook_limit=50, tenant_limit=1)
        request = _make_request("/billing/webhooks/fake", tenant=None)

        statuses = [(await middleware.dispatch(request, _call_next)).status_code for _ in range(5)]

        assert statuses == [200] * 5

    async def test_the_refusal_names_no_limit(self, monkeypatch):
        middleware = self._middleware(monkeypatch, webhook_limit=1)
        request = _make_request("/billing/webhooks/fake", tenant=None)
        await middleware.dispatch(request, _call_next)

        response = await middleware.dispatch(request, _call_next)

        assert response.status_code == 429 and b"per minute" not in response.body

    async def test_chat_limits_are_unaffected_by_webhook_traffic(self, monkeypatch):
        middleware = self._middleware(monkeypatch, webhook_limit=1, tenant_limit=1)
        await middleware.dispatch(_make_request("/billing/webhooks/fake", tenant=None), _call_next)

        assert (await middleware.dispatch(_make_request("/chat/stream/queued"), _call_next)).status_code == 200
