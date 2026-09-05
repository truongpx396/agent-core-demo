"""Tests for app/api/rate_limit.py's per-tenant HTTP rate limiting.

An in-memory `limits` storage backend (`memory://`) stands in for Redis —
hermetic, no live service needed, same "fake the collaborator, not the
network" approach the rest of this suite uses. The middleware's
`dispatch()` is called directly (via asyncio.run — no pytest-asyncio
plugin here, see tests/turns/test_queue.py's own module docstring) with a
hand-built Request, the same "test the function, not the framework
wiring" approach tests/api/test_api.py's module docstring already establishes
for this codebase's route handlers.
"""
import asyncio

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.requests import Request
from starlette.responses import Response

from app.api import rate_limit
from app.core import metrics
from tests.conftest import metric_value


def _make_request(path: str, *, tenant: str | None = "acme", client_host="10.0.0.1") -> Request:
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
    def test_unrated_path_is_never_limited(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/health")
        # Three "hits" against a 1/minute limit would fail if this path
        # were rate-limited at all — GET /health must never be.
        for _ in range(3):
            response = asyncio.run(middleware.dispatch(request, _call_next))
            assert response.status_code == 200

    def test_allows_up_to_the_limit_then_rejects_with_429(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=2)
        request = _make_request("/chat/stream/queued")

        first = asyncio.run(middleware.dispatch(request, _call_next))
        second = asyncio.run(middleware.dispatch(request, _call_next))
        third = asyncio.run(middleware.dispatch(request, _call_next))

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429

    def test_different_tenants_get_independent_budgets(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        acme_request = _make_request("/chat/stream/queued", tenant="acme")
        other_request = _make_request("/chat/stream/queued", tenant="other-tenant")

        assert asyncio.run(middleware.dispatch(acme_request, _call_next)).status_code == 200
        # acme is now over budget — a DIFFERENT tenant hitting the same
        # path must not be affected by acme's own count.
        assert asyncio.run(middleware.dispatch(acme_request, _call_next)).status_code == 429
        assert asyncio.run(middleware.dispatch(other_request, _call_next)).status_code == 200

    def test_missing_tenant_header_falls_back_to_client_address(self, monkeypatch):
        """Doesn't crash the limiter — the request is about to be rejected
        with 422 by get_ctx's own dependency anyway once it actually
        reaches a real endpoint."""
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/chat/stream/queued", tenant=None, client_host="10.0.0.9")
        response = asyncio.run(middleware.dispatch(request, _call_next))
        assert response.status_code == 200

    def test_storage_failure_fails_open_not_closed(self, monkeypatch):
        """Redis (or here, the storage backend) being unreachable must
        never block the core turn — same degrade-don't-crash posture as
        app/retrieval/semantic_cache.py and app/agent/moderation.py."""
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)

        def _broken_hit(*args, **kwargs):
            raise ConnectionError("storage unreachable")

        monkeypatch.setattr(rate_limit._strategy, "hit", _broken_hit)
        request = _make_request("/chat/stream/queued")

        response = asyncio.run(middleware.dispatch(request, _call_next))
        assert response.status_code == 200

    def test_rejection_increments_the_metric(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, limit_per_minute=1)
        request = _make_request("/chat/stream/queued")
        before = metric_value(metrics.agent_rate_limit_exceeded_total)

        asyncio.run(middleware.dispatch(request, _call_next))  # consumes the budget
        asyncio.run(middleware.dispatch(request, _call_next))  # rejected

        after = metric_value(metrics.agent_rate_limit_exceeded_total)
        assert after == before + 1
