"""The hermetic suite never calls the ML service's /v1/decide, even when a test turns the router ON.

`tests/conftest.py::mock_classification` (a fake bound to the `RouteClassifier` port) is the guard (Constitution VII: a new live-service call on the turn path gets an autouse mock). A
guard nobody tests is a comment, so this proves two things: with it in place a full-graph turn with the router on makes no HTTP request at
all, and with the real adapter bound the same turn WOULD make one (so the first claim could have failed).
"""
import httpx
from langchain_core.messages import AIMessage, HumanMessage

from app.adapters.mlservice import classification
from app.adapters.mlservice.classification import MlServiceClassifier
from app.agent.graph import model_route
from app.agent.graph.build import GraphDeps
from app.ports.classification import route_classifier
from tests.agent.fakes import cfg, queue_llm
from tests.ecorp import build_graph


def _router_on(monkeypatch):
    monkeypatch.setattr(model_route, "ROUTER_MODE", "on")
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", "chat-strong")


def _record_http(monkeypatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"error": {"code": "model_unavailable", "message": "no model can answer these questions right now"}})

    monkeypatch.setattr(classification.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(recording), **kw))
    return seen


async def _one_turn():
    graph = build_graph(GraphDeps(llm=queue_llm(AIMessage(content="A long enough local answer."))))
    return await graph.ainvoke({"messages": [HumanMessage(content="what are the support hours?")]}, config=cfg())


async def test_a_full_turn_with_the_router_on_makes_no_classify_request_in_the_default_suite(monkeypatch):
    _router_on(monkeypatch)
    requests = _record_http(monkeypatch)
    assert not isinstance(route_classifier.get(), MlServiceClassifier), "the autouse mock_classification guard is not in place"

    result = await _one_turn()

    assert result["messages"][-1].content == "A long enough local answer." and requests == []


async def test_the_same_turn_would_call_classify_if_the_guard_were_removed(monkeypatch):
    _router_on(monkeypatch)
    requests = _record_http(monkeypatch)

    with route_classifier.overriding(MlServiceClassifier()):
        await _one_turn()

    assert [r.url.path for r in requests] == ["/v1/decide"]
