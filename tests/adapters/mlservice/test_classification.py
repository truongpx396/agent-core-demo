"""app/core/classification.py: one typed question out, one validated answer back, and every failure a value (specs/015 FR-024, specs/016).

`httpx.MockTransport` stands in for the decision service, so the REAL client, the real deadline handling and the real parsing run; only the network
is replaced. Two kinds of answer are fed in: the local service's (with its `x_` evidence), and one built from the hosted System One / Jev interface's
DOCUMENTATION (Opper's compatibility page: `choice`, `confidence`, `probabilities`, `usage`). The second is not a recording of a real response, which
nobody has made yet; it pins the fields the swap relies on, so a change that made the client need an `x_` field would fail here before it failed in
front of a hosted service.
"""
import asyncio
import json
import logging

import httpx
import pytest

from app.adapters.mlservice import classification
from app.ports.classification import NO_ANSWER, Choice, Labelled

REAL_CLIENT = httpx.AsyncClient
QUESTION = Choice(
    "route-topics", "What kind of work does the message ask for?",
    {"code": "writing or fixing code", "lookup": "finding a written fact", "smalltalk": "courtesy with no task"},
)
LOCAL = {
    "model": "bge-small-en-v1.5", "x_served_by": "primary", "usage": {"input_tokens": 9, "output_tokens": 0},
    "answers": {"route-topics": {
        "type": "choice", "choice": "code", "x_labelset_version": "6a3fe2f35057",
        "x_evidence": {"nearest": "code", "similarity": 0.773, "runner_up": "lookup", "margin": 0.041, "truncated": False},
    }},
}
HOSTED = {  # built from the documented fields only
    "model": "typesafe/jev-1.13.0", "usage": {"input_tokens": 61, "output_tokens": 0},
    "answers": {"route-topics": {"type": "choice", "choice": "lookup", "confidence": 0.91, "probabilities": {"code": 0.04, "lookup": 0.91, "smalltalk": 0.05}}},
}


@pytest.fixture
def serve(monkeypatch):
    """Install a handler as the service; returns the list of requests it saw."""
    seen: list[httpx.Request] = []

    def install(handler):
        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(classification.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(recording), **kw))
        return seen

    return install


async def ask(text="prove it"):
    return await classification.choose(QUESTION, text)


def with_answer(**fields):
    return {**LOCAL, "answers": {"route-topics": {**LOCAL["answers"]["route-topics"], **fields}}}


async def test_a_healthy_local_answer_becomes_a_label_with_its_evidence(serve):
    serve(lambda request: httpx.Response(200, json=LOCAL))

    assert await ask() == Labelled("labelled", "code", "code", 0.773, 0.041, "primary", "6a3fe2f35057")


async def test_an_answer_with_only_the_documented_fields_works_too_which_is_what_makes_the_swap_a_config_change(serve):
    serve(lambda request: httpx.Response(200, json=HOSTED))

    got = await ask()

    assert got == Labelled("labelled", "lookup", None, None, None, "remote", None, 0.91)
    assert got.label == "lookup" and got.confidence == 0.91


async def test_the_request_is_the_question_and_the_users_message_and_nothing_else(serve):
    seen = serve(lambda request: httpx.Response(200, json=LOCAL))

    await ask("what is 2+2?")

    (request,) = seen
    assert request.method == "POST" and request.url.path == "/v1/decide"
    assert json.loads(request.content) == {  # no tenant, principal, history, tools or context; no `model`, so the service's default answers
        "state": "what is 2+2?",
        "questions": {"route-topics": {
            "type": "choice", "instructions": QUESTION.instructions, "criteria": dict(QUESTION.options),
        }},
    }
    assert {"authorization", "x-tenant-id", "x-principal-id"}.isdisjoint({k.lower() for k in request.headers})


async def test_a_long_message_is_cut_to_its_opening_not_refused(serve):
    seen = serve(lambda request: httpx.Response(200, json=LOCAL))

    await ask("word " * 2000)

    assert len(json.loads(seen[0].content)["state"]) == classification.MAX_CHARS == 2000


async def test_an_abstention_is_a_choice_of_none_with_the_nearest_still_reported(serve):
    serve(lambda request: httpx.Response(200, json=with_answer(choice=None, x_evidence={"nearest": "smalltalk", "similarity": 0.4, "margin": 0.0})))

    got = await ask()

    assert got.outcome == "labelled" and got.label is None and got.nearest == "smalltalk" and got.similarity == 0.4


async def test_the_fallback_models_answer_is_an_answer_too_and_says_who_gave_it(serve):
    serve(lambda request: httpx.Response(200, json={**LOCAL, "x_served_by": "fallback"}))

    assert (await ask()).served_by == "fallback"


async def test_unavailable_is_no_answer_not_an_error(serve):
    serve(lambda request: httpx.Response(503, json={"error": {"code": "model_unavailable", "message": "no model can answer these questions right now"}}))

    assert await ask() == NO_ANSWER


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": {"code": "overloaded"}}),  # a 503 that is not the interface's own "cannot answer now" is a failure
        httpx.Response(503, content=b"<html>Bad gateway</html>"),
        httpx.Response(503, json=["not", "an", "error"]),
        httpx.Response(500, json={"error": {"code": "internal"}}),
        httpx.Response(500, json={"error": {"code": "model_unavailable"}}),  # the code means "cannot answer now" only on the 503 that carries it
        httpx.Response(529, json={"error": {"code": "overloaded"}}),
        httpx.Response(201, json=LOCAL),
        httpx.Response(400, json={"error": {"code": "criteria_mismatch"}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={**LOCAL, "answers": []}),
        httpx.Response(200, json={**LOCAL, "answers": {}}),
        httpx.Response(200, json={**LOCAL, "answers": {"another-question": LOCAL["answers"]["route-topics"]}}),  # an answer to a question that was not asked
        httpx.Response(200, json=with_answer(type="noul")),
        httpx.Response(200, json=with_answer(type=None)),
        httpx.Response(200, json=with_answer(choice="hacking")),  # not one of the options offered
        httpx.Response(200, json=with_answer(choice=3)),
        httpx.Response(200, json=with_answer(choice="")),
        httpx.Response(200, json={**LOCAL, "x_served_by": "somebody-else"}),
        httpx.Response(200, json=with_answer(x_evidence={"nearest": "code", "similarity": "high", "margin": 0.1})),
        httpx.Response(200, json=with_answer(x_evidence={"nearest": "code", "similarity": True, "margin": 0.1})),
        httpx.Response(200, json=with_answer(x_evidence={"nearest": "code", "similarity": 1.5, "margin": 0.1})),
        httpx.Response(200, json=with_answer(x_evidence={"nearest": "code", "similarity": 0.5, "margin": -0.1})),
        httpx.Response(200, json=with_answer(confidence=1.5)),
        httpx.Response(200, json=with_answer(confidence="sure")),
        httpx.Response(200, json=with_answer(confidence=True)),
        httpx.Response(200, content=b'{"answers": {"route-topics": {"type": "choice", "choice": "code", "x_evidence": {"similarity": NaN}}}}'),
    ],
)
async def test_anything_but_the_documented_answer_is_an_error_never_a_label(serve, response):
    serve(lambda request: response)

    assert (await ask()).outcome == "error"


async def test_evidence_is_optional_but_if_offered_it_is_checked(serve):
    serve(lambda request: httpx.Response(200, json=with_answer(x_evidence=None)))

    assert await ask() == Labelled("labelled", "code", None, None, None, "primary", "6a3fe2f35057")


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
