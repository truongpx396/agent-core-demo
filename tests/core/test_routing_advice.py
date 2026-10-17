"""app/core/routing_advice.py: one question out, one validated score back, and every failure a value.

`httpx.MockTransport` stands in for the ML service, so the REAL client, the real deadline handling and the real parsing run; only the
network is replaced.
"""
import asyncio
import json
import logging

import httpx
import pytest

from app.core import routing_advice
from app.core.routing_advice import NO_ADVICE, Advice, advise

REAL_CLIENT = httpx.AsyncClient
GOOD = {"served_by": "primary", "model": "bge-small-en-v1.5", "head_version": "v7",
        "results": [{"index": 0, "decision": "needs_stronger", "score": 0.83, "truncated": False}]}


@pytest.fixture
def serve(monkeypatch):
    """Install a handler as the ML service; returns the list of requests it saw."""
    seen: list[httpx.Request] = []

    def install(handler):
        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(routing_advice.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(recording), **kw))
        return seen

    return install


async def test_a_healthy_answer_becomes_advice(serve):
    serve(lambda request: httpx.Response(200, json=GOOD))

    assert await advise("prove it") == Advice("advice", 0.83, "primary", "v7")


async def test_the_request_carries_the_users_message_and_nothing_else(serve):
    seen = serve(lambda request: httpx.Response(200, json=GOOD))

    await advise("what is 2+2?")

    (request,) = seen
    assert request.method == "POST" and request.url.path == "/classify"
    assert json.loads(request.content) == {"task": "route", "texts": ["what is 2+2?"]}  # no tenant, principal, history, tools or context
    assert {"authorization", "x-tenant-id", "x-principal-id"}.isdisjoint({k.lower() for k in request.headers})


async def test_the_fallback_models_answer_is_advice_too_and_says_who_gave_it(serve):
    serve(lambda request: httpx.Response(200, json={**GOOD, "served_by": "fallback"}))

    assert (await advise("x")).served_by == "fallback"


async def test_none_served_is_no_advice_not_an_error(serve):
    serve(lambda request: httpx.Response(200, json={"served_by": "none", "model": None, "head_version": None,
                                                      "results": [{"index": 0, "decision": "none", "score": None, "truncated": False}]}))

    assert await advise("x") == NO_ADVICE


async def test_a_null_score_from_a_served_model_is_no_advice(serve):
    serve(lambda request: httpx.Response(200, json={**GOOD, "results": [{"index": 0, "decision": "none", "score": None}]}))

    assert (await advise("x")).outcome == "no_advice"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": {"code": "internal"}}),
        httpx.Response(503, json=GOOD),  # a proxy's error page that happens to carry a valid-looking body is still not advice
        httpx.Response(201, json=GOOD),
        httpx.Response(400, json={"error": {"code": "invalid_input"}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"served_by": "primary"}),
        httpx.Response(200, json={**GOOD, "results": []}),
        httpx.Response(200, json={**GOOD, "results": [{"score": 0.1}, {"score": 0.2}]}),
        httpx.Response(200, json={**GOOD, "served_by": "somebody-else"}),
        httpx.Response(200, json={**GOOD, "results": [{"score": "high"}]}),
        httpx.Response(200, json={**GOOD, "results": [{"score": True}]}),
        httpx.Response(200, json={**GOOD, "results": [{"score": 1.5}]}),
        httpx.Response(200, json={**GOOD, "results": [{"score": -0.1}]}),
        httpx.Response(200, content=b'{"served_by": "primary", "results": [{"score": NaN}]}'),
    ],
)
async def test_anything_but_the_documented_answer_is_an_error_never_advice(serve, response):
    serve(lambda request: response)

    assert (await advise("x")).outcome == "classifier_error"


async def test_a_refused_connection_is_an_error(serve):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    serve(refuse)

    assert (await advise("x")).outcome == "classifier_error"


async def test_a_httpx_timeout_is_a_timeout(serve):
    def slow(request):
        raise httpx.ReadTimeout("too slow")

    serve(slow)

    assert (await advise("x")).outcome == "classifier_timeout"


async def test_a_peer_that_never_finishes_is_cut_off_by_the_wall_clock_bound(serve, monkeypatch):
    # httpx's own timeouts are per phase, so a peer that trickles bytes can outlast them; the whole exchange has a hard bound
    monkeypatch.setattr(routing_advice, "ROUTER_TIMEOUT_SECONDS", 0.05)

    async def hang(request):
        await asyncio.Event().wait()

    serve(hang)
    started = asyncio.get_running_loop().time()

    result = await advise("x")

    assert result.outcome == "classifier_timeout" and asyncio.get_running_loop().time() - started < 1.0


async def test_it_never_raises_whatever_the_transport_does(serve):
    def explode(request):
        raise RuntimeError("something nobody planned for")

    serve(explode)

    assert (await advise("x")).outcome == "classifier_error"


async def test_the_users_text_never_reaches_a_log(serve, caplog):
    secret = "my-password-is-hunter2"
    caplog.set_level(logging.DEBUG)

    def explode(request):
        raise RuntimeError(f"failed while handling {request.content!r}")  # a hostile/buggy transport that echoes the body

    serve(explode)
    await advise(secret)
    serve(lambda request: httpx.Response(500, text=secret))
    await advise(secret)

    assert "hunter2" not in caplog.text and "hunter2" not in repr([r.__dict__ for r in caplog.records])
    assert any(r.__dict__.get("error_class") == "RuntimeError" for r in caplog.records)  # the class is logged, which is enough to debug
