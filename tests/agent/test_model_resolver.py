"""Tests for app/agent/model_resolver.py.

Two kinds of test, on purpose:

  * lookup behaviour (cache, negative cache, error handling) runs against
    `httpx.MockTransport`, so the real request-building code is exercised with
    no network;
  * "does not block the event loop" runs against a real, deliberately slow HTTP
    server on 127.0.0.1. A mock cannot show this: the original defect was a
    synchronous `httpx.get` inside code that runs on the event loop, and only a
    genuinely slow response lets a heartbeat measure whether the loop kept
    running while it waited (spec 008, B19: a 1.02 s stall, and the failed
    lookup repeated on every later call).
"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.agent import model_resolver

_MODEL_INFO = {"data": [{"model_name": "chat", "litellm_params": {"model": "ollama_chat/qwen2.5:3b"}}]}


@pytest.fixture(autouse=True)
def _fresh_resolver_state():
    model_resolver._cache.clear()
    model_resolver._failed_at.clear()
    yield
    model_resolver._cache.clear()
    model_resolver._failed_at.clear()


def _serve(monkeypatch, handler):
    """Route the resolver's own `httpx.AsyncClient` through a MockTransport,
    returning the list of requests it received."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        model_resolver.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs),
    )
    return seen


def _ok(request):
    return httpx.Response(200, json=_MODEL_INFO)


class TestResolveModel:
    async def test_resolves_a_known_alias(self, monkeypatch):
        _serve(monkeypatch, _ok)

        assert await model_resolver.resolve_model("chat") == "ollama_chat/qwen2.5:3b"

    async def test_asks_the_proxy_admin_endpoint_with_the_api_key(self, monkeypatch):
        monkeypatch.setattr(model_resolver, "OPENAI_API_BASE", "http://litellm.test:4000/v1")
        monkeypatch.setattr(model_resolver, "OPENAI_API_KEY", "sk-test")
        seen = _serve(monkeypatch, _ok)

        await model_resolver.resolve_model("chat")

        (request,) = seen
        assert str(request.url) == "http://litellm.test:4000/model/info"
        assert request.headers["authorization"] == "Bearer sk-test"

    async def test_unknown_alias_returns_none(self, monkeypatch):
        _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))

        assert await model_resolver.resolve_model("nonexistent") is None

    async def test_caches_after_first_successful_lookup(self, monkeypatch):
        seen = _serve(monkeypatch, _ok)

        for _ in range(3):
            await model_resolver.resolve_model("chat")

        assert len(seen) == 1

    async def test_degrades_to_none_on_connection_failure(self, monkeypatch):
        def refuse(request):
            raise httpx.ConnectError("LiteLLM unreachable")

        _serve(monkeypatch, refuse)

        assert await model_resolver.resolve_model("chat") is None

    async def test_degrades_to_none_on_http_error_status(self, monkeypatch):
        _serve(monkeypatch, lambda request: httpx.Response(500))

        assert await model_resolver.resolve_model("chat") is None

    async def test_degrades_to_none_on_a_malformed_body(self, monkeypatch):
        _serve(monkeypatch, lambda request: httpx.Response(200, content=b"<html>not json</html>"))

        assert await model_resolver.resolve_model("chat") is None

    def test_admin_base_url_strips_the_v1_suffix(self, monkeypatch):
        monkeypatch.setattr(model_resolver, "OPENAI_API_BASE", "http://localhost:4000/v1")
        assert model_resolver._admin_base_url() == "http://localhost:4000"


class TestAFailedLookupIsNotRepeatedOnEveryCall:
    """While LiteLLM is down, every recorded turn used to pay a fresh 5 s
    timeout for an answer that could not have changed."""

    async def test_a_failure_is_not_retried_within_the_window(self, monkeypatch):
        def refuse(request):
            raise httpx.ConnectError("LiteLLM unreachable")

        seen = _serve(monkeypatch, refuse)

        for _ in range(5):
            assert await model_resolver.resolve_model("chat") is None

        assert len(seen) == 1

    async def test_an_unknown_alias_is_not_looked_up_again_within_the_window(self, monkeypatch):
        seen = _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))

        for _ in range(3):
            await model_resolver.resolve_model("nonexistent")

        assert len(seen) == 1

    async def test_the_lookup_is_retried_once_the_window_has_passed(self, monkeypatch):
        outage = {"on": True}

        def flaky(request):
            if outage["on"]:
                raise httpx.ConnectError("LiteLLM unreachable")
            return httpx.Response(200, json=_MODEL_INFO)

        seen = _serve(monkeypatch, flaky)
        now = {"t": 1_000.0}
        monkeypatch.setattr(model_resolver.time, "monotonic", lambda: now["t"])

        assert await model_resolver.resolve_model("chat") is None
        outage["on"] = False
        now["t"] += model_resolver.FAILED_LOOKUP_RETRY_SECONDS - 1
        assert await model_resolver.resolve_model("chat") is None  # still inside the window
        assert len(seen) == 1

        now["t"] += 2
        assert await model_resolver.resolve_model("chat") == "ollama_chat/qwen2.5:3b"
        assert len(seen) == 2

    async def test_one_aliass_failure_does_not_suppress_another(self, monkeypatch):
        def only_chat(request):
            return httpx.Response(200, json=_MODEL_INFO)

        _serve(monkeypatch, only_chat)

        assert await model_resolver.resolve_model("missing") is None
        assert await model_resolver.resolve_model("chat") == "ollama_chat/qwen2.5:3b"


class _SlowModelInfo(BaseHTTPRequestHandler):
    delay_seconds = 0.5

    def do_GET(self):  # noqa: N802 - http.server's required name
        time.sleep(self.delay_seconds)
        body = json.dumps(_MODEL_INFO).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def slow_litellm(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowModelInfo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(model_resolver, "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1")
    yield _SlowModelInfo.delay_seconds
    server.shutdown()
    server.server_close()


async def test_resolving_a_model_does_not_stall_the_event_loop(slow_litellm):
    """A heartbeat ticks every 10 ms while a half-second lookup runs. A loop
    left free keeps ticking; one blocked inside the lookup shows a gap of about
    the lookup's length."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    resolved = await model_resolver.resolve_model("chat")
    stop.set()
    await task

    assert resolved == "ollama_chat/qwen2.5:3b", "test setup: the slow server must actually have answered"
    assert max(gaps) < slow_litellm * 0.8, (
        f"the event loop was unresponsive for {max(gaps):.2f}s while a model resolved"
    )
