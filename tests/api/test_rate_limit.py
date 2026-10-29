"""Tests for app/api/rate_limit.py: the payment webhook's per-source-address limit, and the absence of every other limit.

Spec 018 moved the per-tenant limit on turn-creating requests out of this process and to the front door (services/authlimit), which enforces it
from a verified tenant and person as a token bucket. This file used to test that limiter; it now tests that the API does NOT throttle turns any
more (a second policy point, keyed on a header the API no longer trusts), and that the one limit it keeps still works.

An in-memory `limits` storage backend (`memory://`) stands in for Redis: hermetic, no live service needed, the same "fake the collaborator, not the
network" approach the rest of this suite uses. The middleware's `dispatch()` is called directly with a hand-built Request, the same "test the
function, not the framework wiring" approach tests/api/test_api.py's module docstring establishes for this codebase's route handlers.
"""

from limits import RateLimitItemPerMinute, storage, strategies
from starlette.requests import Request
from starlette.responses import Response

from app.api import rate_limit
from app.core import config, metrics
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


def _fresh_middleware(monkeypatch, *, webhook_limit: int):
    """A middleware instance backed by its own isolated in-memory store (not the real module-level singleton, which would leak counts between
    tests), with the webhook ceiling a test wants."""
    mem_storage = storage.storage_from_string("memory://")
    monkeypatch.setattr(rate_limit, "_strategy", strategies.MovingWindowRateLimiter(mem_storage))
    monkeypatch.setattr(rate_limit, "_webhook_limit", RateLimitItemPerMinute(webhook_limit))
    return rate_limit.WebhookRateLimitMiddleware(app=None)


class TestTheWebhookRoute:
    """POST /billing/webhooks/{provider} has no tenant (the caller is a payment provider) and Caddy lets it through un-checked, so it is
    limited per SOURCE ADDRESS with its own ceiling (app/api/rate_limit.py)."""

    async def test_it_is_limited_per_source_address(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, webhook_limit=2)
        provider_ip = _make_request("/billing/webhooks/fake", tenant=None, client_host="203.0.113.7")
        other_ip = _make_request("/billing/webhooks/fake", tenant=None, client_host="198.51.100.9")

        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 200
        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 200
        assert (await middleware.dispatch(provider_ip, _call_next)).status_code == 429
        assert (await middleware.dispatch(other_ip, _call_next)).status_code == 200, "one source's flood is not another's"

    async def test_a_tenant_header_on_the_webhook_route_is_never_a_reason_to_key_on_it(self, monkeypatch):
        """The header is the sender's own claim: rotating it must not buy a fresh budget."""
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)

        first = _make_request("/billing/webhooks/fake", tenant="a", client_host="203.0.113.7")
        second = _make_request("/billing/webhooks/fake", tenant="b", client_host="203.0.113.7")

        assert (await middleware.dispatch(first, _call_next)).status_code == 200
        assert (await middleware.dispatch(second, _call_next)).status_code == 429

    async def test_the_refusal_names_no_limit(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)
        request = _make_request("/billing/webhooks/fake", tenant=None)
        await middleware.dispatch(request, _call_next)

        response = await middleware.dispatch(request, _call_next)

        assert response.status_code == 429 and b"per minute" not in response.body

    async def test_a_rejection_increments_the_metric(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)
        request = _make_request("/billing/webhooks/fake", tenant=None)
        before = metric_value(metrics.agent_rate_limit_exceeded_total)

        await middleware.dispatch(request, _call_next)  # consumes the budget
        await middleware.dispatch(request, _call_next)  # rejected

        assert metric_value(metrics.agent_rate_limit_exceeded_total) == before + 1

    async def test_storage_failure_fails_open_not_closed(self, monkeypatch):
        """Redis (or here, the storage backend) being unreachable must never block a payment webhook: the signature, not this limit, is the
        defence, and a provider's redelivery is the retry."""
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)

        def _broken_hit(*args, **kwargs):
            raise ConnectionError("storage unreachable")

        monkeypatch.setattr(rate_limit._strategy, "hit", _broken_hit)

        response = await middleware.dispatch(_make_request("/billing/webhooks/fake", tenant=None), _call_next)

        assert response.status_code == 200


class TestTheApiNoLongerLimitsTurns:
    """The decision of spec 018 (FR-029), pinned so it is not quietly undone: how fast a tenant or a person may
    start turns is the front door's to decide.
    """

    async def test_a_burst_of_turn_creating_requests_is_never_throttled_by_the_api_itself(self, monkeypatch):
        # A webhook ceiling of 1 would reject the second hit if these paths shared it, and the old per-tenant limit was 30 a minute.
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)
        before = metric_value(metrics.agent_rate_limit_exceeded_total)

        for path in ("/chat/stream/queued", "/chat/resume", "/ingest/upload"):
            for _ in range(200):
                response = await middleware.dispatch(_make_request(path, tenant="ecorp"), _call_next)
                assert response.status_code == 200, f"{path} was throttled by the API"

        assert metric_value(metrics.agent_rate_limit_exceeded_total) == before

    async def test_no_other_path_is_limited_either(self, monkeypatch):
        middleware = _fresh_middleware(monkeypatch, webhook_limit=1)

        for path in ("/health", "/chat/cancel", "/usage", "/chat/sessions"):
            for _ in range(5):
                assert (await middleware.dispatch(_make_request(path), _call_next)).status_code == 200, path

    def test_the_app_mounts_the_webhook_limit_and_no_tenant_limiter(self):
        from app.api.main import app

        mounted = {m.cls.__name__ for m in app.user_middleware}

        assert "WebhookRateLimitMiddleware" in mounted
        assert not {name for name in mounted if "RateLimit" in name} - {"WebhookRateLimitMiddleware"}

    def test_the_tenant_limit_and_its_setting_are_gone(self):
        assert not hasattr(config, "RATE_LIMIT_PER_MINUTE")
        assert "rate_limit_per_minute" not in config.Settings.model_fields
        assert not hasattr(rate_limit, "RATE_LIMITED_PATHS") and not hasattr(rate_limit, "_tenant_key")
