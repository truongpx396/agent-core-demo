"""app/core/classification.py: one question out, one validated answer back, and every failure a value (specs/015 FR-024).

`httpx.MockTransport` stands in for the ML service, so the REAL client, the real deadline handling and the real parsing run; only the network is
replaced.
"""
import asyncio
import json
import logging

import httpx
import pytest

from app.core import classification
from app.core.classification import NO_ANSWER, Labelled
from tests.conftest import REAL_LABEL

REAL_CLIENT = httpx.AsyncClient
GOOD = {
    "served_by": "primary", "model": "bge-small-en-v1.5", "labelset": "route-topics", "labelset_version": "6a3fe2f35057",
    "results": [{"index": 0, "label": "code", "nearest": "code", "similarity": 0.773, "runner_up": "lookup", "margin": 0.041, "truncated": False}],
}


@pytest.fixture
def serve(monkeypatch):
    """Install a handler as the ML service; returns the list of requests it saw. The suite's autouse guard replaced `label`, so the real one is put back."""
    seen: list[httpx.Request] = []
    monkeypatch.setattr(classification, "label", REAL_LABEL)

    def install(handler):
        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(classification.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(recording), **kw))
        return seen

    return install


async def ask(text="prove it"):
    return await classification.label("route-topics", text)


async def test_a_healthy_answer_becomes_a_label(serve):
    serve(lambda request: httpx.Response(200, json=GOOD))

    assert await ask() == Labelled("labelled", "code", "code", 0.773, 0.041, "primary", "6a3fe2f35057")


async def test_the_request_carries_the_set_name_and_the_users_message_and_nothing_else(serve):
    seen = serve(lambda request: httpx.Response(200, json=GOOD))

    await ask("what is 2+2?")

    (request,) = seen
    assert request.method == "POST" and request.url.path == "/classify"
    assert json.loads(request.content) == {"task": "label", "labelset": "route-topics", "texts": ["what is 2+2?"]}  # no tenant, principal, history, tools or context
    assert {"authorization", "x-tenant-id", "x-principal-id"}.isdisjoint({k.lower() for k in request.headers})


async def test_a_long_message_is_cut_to_its_opening_not_refused(serve):
    seen = serve(lambda request: httpx.Response(200, json=GOOD))

    await ask("word " * 2000)

    assert len(json.loads(seen[0].content)["texts"][0]) == classification.MAX_CHARS == 2000


async def test_an_abstention_is_a_label_of_none_with_the_nearest_still_reported(serve):
    abstained = {**GOOD, "results": [{**GOOD["results"][0], "label": None, "nearest": "smalltalk", "similarity": 0.4, "margin": 0.0}]}
    serve(lambda request: httpx.Response(200, json=abstained))

    got = await ask()

    assert got.outcome == "labelled" and got.label is None and got.nearest == "smalltalk" and got.similarity == 0.4


async def test_the_fallback_models_answer_is_an_answer_too_and_says_who_gave_it(serve):
    serve(lambda request: httpx.Response(200, json={**GOOD, "served_by": "fallback"}))

    assert (await ask()).served_by == "fallback"


async def test_none_served_is_no_answer_not_an_error(serve):
    nothing = {"served_by": "none", "model": None, "labelset": "route-topics", "labelset_version": None,
               "results": [{"index": 0, "label": None, "nearest": None, "similarity": None, "runner_up": None, "margin": None, "truncated": False}]}
    serve(lambda request: httpx.Response(200, json=nothing))

    assert await ask() == NO_ANSWER


def with_result(**fields):
    return {**GOOD, "results": [{**GOOD["results"][0], **fields}]}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": {"code": "internal"}}),
        httpx.Response(503, json=GOOD),  # a proxy's error page that happens to carry a valid-looking body is still not an answer
        httpx.Response(201, json=GOOD),
        httpx.Response(400, json={"error": {"code": "unknown_labelset"}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"served_by": "primary"}),
        httpx.Response(200, json={**GOOD, "results": []}),
        httpx.Response(200, json={**GOOD, "results": [GOOD["results"][0], GOOD["results"][0]]}),
        httpx.Response(200, json={**GOOD, "served_by": "somebody-else"}),
        httpx.Response(200, json={**GOOD, "labelset": "another-set"}),  # an answer for a set that was not asked about
        httpx.Response(200, json=with_result(similarity="high")),
        httpx.Response(200, json=with_result(similarity=True)),
        httpx.Response(200, json=with_result(similarity=1.5)),
        httpx.Response(200, json=with_result(similarity=None)),
        httpx.Response(200, json=with_result(margin=-0.1)),
        httpx.Response(200, json=with_result(label=3)),
        httpx.Response(200, json=with_result(nearest=None)),
        httpx.Response(200, content=b'{"served_by": "primary", "results": [{"similarity": NaN, "margin": 0, "nearest": "a", "label": "a"}]}'),
    ],
)
async def test_anything_but_the_documented_answer_is_an_error_never_a_label(serve, response):
    serve(lambda request: response)

    assert (await ask()).outcome == "error"


async def test_a_refused_connection_is_an_error(serve):
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    serve(refuse)

    assert (await ask()).outcome == "error"


async def test_a_httpx_timeout_is_a_timeout(serve):
    def slow(request):
        raise httpx.ReadTimeout("too slow")

    serve(slow)

    assert (await ask()).outcome == "timeout"


async def test_a_peer_that_never_finishes_is_cut_off_by_the_wall_clock_bound(serve, monkeypatch):
    # httpx's own timeouts are per phase, so a peer that trickles bytes can outlast them; the whole exchange has a hard bound
    monkeypatch.setattr(classification, "ROUTER_TIMEOUT_SECONDS", 0.05)

    async def hang(request):
        await asyncio.Event().wait()

    serve(hang)
    started = asyncio.get_running_loop().time()

    result = await ask()

    assert result.outcome == "timeout" and asyncio.get_running_loop().time() - started < 1.0


async def test_it_never_raises_whatever_the_transport_does(serve):
    def explode(request):
        raise RuntimeError("something nobody planned for")

    serve(explode)

    assert (await ask()).outcome == "error"


async def test_the_users_text_never_reaches_a_log(serve, caplog):
    secret = "my-password-is-hunter2"
    caplog.set_level(logging.DEBUG)

    def explode(request):
        raise RuntimeError(f"failed while handling {request.content!r}")  # a hostile/buggy transport that echoes the body

    serve(explode)
    await ask(secret)
    serve(lambda request: httpx.Response(500, text=secret))
    await ask(secret)

    assert "hunter2" not in caplog.text and "hunter2" not in repr([r.__dict__ for r in caplog.records])
    assert any(r.__dict__.get("error_class") == "RuntimeError" for r in caplog.records)  # the class is logged, which is enough to debug
