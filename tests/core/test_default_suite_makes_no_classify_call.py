"""The hermetic suite never calls the ML service's /classify, even when a test turns the router ON.

`tests/conftest.py::mock_routing_advice` is the guard (Constitution VII: a new live-service call on the turn path gets an autouse mock). A
guard nobody tests is a comment, so this proves two things: with it in place a full-graph turn with the router enabled makes no HTTP request
at all, and with the real `advise` put back the same turn WOULD make one (so the first claim could have failed).
"""
import httpx
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import model_route
from app.agent.graph.build import GraphDeps
from app.core import routing_advice
from tests.agent.fakes import cfg, queue_llm
from tests.conftest import REAL_ADVISE
from tests.ecorp import build_graph


def _router_on(monkeypatch):
    monkeypatch.setattr(model_route, "ROUTER_ENABLED", True)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", "chat-strong")


def _record_http(monkeypatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"served_by": "none", "results": [{"index": 0, "decision": "none", "score": None}]})

    monkeypatch.setattr(routing_advice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(recording), **kw))
    return seen


async def _one_turn():
    graph = build_graph(GraphDeps(llm=queue_llm(AIMessage(content="A long enough local answer."))))
    return await graph.ainvoke({"messages": [HumanMessage(content="what are the support hours?")]}, config=cfg())


async def test_a_full_turn_with_the_router_enabled_makes_no_classify_request_in_the_default_suite(monkeypatch):
    _router_on(monkeypatch)
    requests = _record_http(monkeypatch)
    assert routing_advice.advise is not REAL_ADVISE, "the autouse mock_routing_advice guard is not in place"

    result = await _one_turn()

    assert result["messages"][-1].content == "A long enough local answer." and requests == []


async def test_the_same_turn_would_call_classify_if_the_guard_were_removed(monkeypatch):
    _router_on(monkeypatch)
    requests = _record_http(monkeypatch)
    monkeypatch.setattr(routing_advice, "advise", REAL_ADVISE)

    await _one_turn()

    assert [r.url.path for r in requests] == ["/classify"]
