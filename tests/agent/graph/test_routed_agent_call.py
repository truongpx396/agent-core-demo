"""`agent()` on a routed turn: which alias is called, which alias is priced and recorded, and what happens when the routed one is not there
(specs/014 contracts/graph-seam.md invariants 1, 4, 5, 7, 8, 9, 10; specs/015 contracts/tiered-router.md invariants 5-7), for BOTH routed tiers.

The rule the tests keep returning to: the ledger and the call can never disagree. A turn the router sent to the medium or strong alias calls it, is
priced as it, and is recorded as it; a call that fell back is priced and recorded as the LOCAL alias that answered, and a call that failed
records nothing. Everything here is hermetic: a recording fake model, a fake price per alias, and (for the one claim only a real client can
settle) the real `ChatOpenAI` over `httpx.MockTransport`.
"""
import ast
import json
from pathlib import Path

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.types import Command

from app.adapters.litellm.failures import OpenAiFailures
from app.agent.cost import metering, pricing
from app.agent.graph import model_route
from app.agent.graph.build import GraphDeps
from app.agent.graph.model_route import routed_unreachable
from app.core import gateway
from app.domains.ecorp import tools as ecorp_tools
from app.ports.classification import Labelled
from tests.agent.fakes import call, cfg, make_agent_node, queue_llm
from tests.conftest import TEST_CTX
from tests.ecorp import build_graph
from tests.fakes.ml import use_classifier

MEDIUM, STRONG, LOCAL = "chat-medium", "chat-strong", "chat"
ALIAS = {"medium": MEDIUM, "strong": STRONG}
REAL_IDEMPOTENT = ecorp_tools.idempotent  # taken once: a spy installed per call would otherwise wrap the previous call's spy
PRICES = {LOCAL: 0.001, MEDIUM: 0.005, STRONG: 0.02}
ROUTED = ["medium", "strong"]

FAILURES = OpenAiFailures()  # the adapter that knows what the SDK's exceptions mean


def _request():
    return httpx.Request("POST", "http://gateway.test/v1/chat/completions")


# The body a real LiteLLM (main-stable) returned, with HTTP 500, when `chat-strong`'s upstream could not be reached (verified against a real
# proxy in front of Ollama, not assumed: the first version of the fallback listed 502/503/504 and would never have fired for this).
_LITELLM_DEAD_UPSTREAM = {"error": {"message": "litellm.APIConnectionError: Ollama_chatException - Cannot connect to host host.docker.internal:9 ssl:<ssl.SSLContext object>", "type": None, "param": None, "code": "500"}}


def status_error(status: int, body=None) -> openai.APIStatusError:
    response = httpx.Response(status, request=_request(), json=body or {})
    return openai.APIStatusError("gateway said no", response=response, body=body)


class Counted:
    """Stands in for a metrics Counter; records the label of every increment."""

    def __init__(self):
        self.labels_seen: list[str] = []

    def labels(self, **kw):
        self.labels_seen.append(kw["outcome"])
        return type("_Bound", (), {"inc": staticmethod(lambda amount=1: None)})()


@pytest.fixture
def priced(monkeypatch):
    """A price per alias, and a record of which alias each call was priced as."""
    asked: list[str] = []

    async def price_call(alias, usage):
        asked.append(alias)
        return pricing.PricedCall(10, 5, 0, 15, PRICES[alias], None)

    monkeypatch.setattr(pricing, "price_call", price_call)
    return asked


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(model_route, "MEDIUM_CHAT_MODEL", MEDIUM)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", STRONG)
    counter = Counted()
    monkeypatch.setattr(model_route.metrics, "agent_model_route_total", counter)
    return counter


class ScriptedLLM:
    """A model whose every `ainvoke` is a scripted outcome: an exception is raised, a message is returned. Records the keywords."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.kwargs: list[dict] = []
        self.messages: list[list] = []

    async def ainvoke(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        self.messages.append(list(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def answer(text="a sufficiently long answer to be a real answer"):
    return AIMessage(content=text, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def node(llm):
    return make_agent_node(llm, model_alias=LOCAL, system_prompt="THE SYSTEM PROMPT")


def turn(route="strong", **extra):
    return {"messages": [HumanMessage(content="a question")], "model_route": route, "run_id": "r1", **extra}


CONFIG = cfg()


def ledger_aliases(usage_event_sink):
    return [row["model_alias"] for row in usage_event_sink]


# ---- the call, the price and the record agree ---------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ROUTED)
async def test_a_routed_turn_calls_prices_and_records_the_tiers_alias(priced, routed, usage_event_sink, tier):
    llm = ScriptedLLM(answer())

    result = await node(llm)(turn(tier), CONFIG)

    assert llm.kwargs[0]["model"] == ALIAS[tier]
    assert priced == [ALIAS[tier]] and ledger_aliases(usage_event_sink) == [ALIAS[tier]]
    assert result["total_cost_usd"] == PRICES[ALIAS[tier]]  # the turn's dollar ceiling sees the routed price too
    assert routed.labels_seen == []


@pytest.mark.parametrize("route", ["local", None])
async def test_a_local_or_undecided_turn_makes_exactly_the_call_it_always_did(priced, routed, usage_event_sink, route):
    llm = ScriptedLLM(answer())

    result = await node(llm)(turn(route), CONFIG)

    assert llm.kwargs[0] == gateway.identity_from_config(CONFIG)  # byte-identical keywords: no `model`, nothing added
    assert priced == [LOCAL] and ledger_aliases(usage_event_sink) == [LOCAL] and result["total_cost_usd"] == PRICES[LOCAL]


async def test_a_checkpoint_from_before_the_route_existed_resumes_and_runs_local(priced, routed, usage_event_sink):
    state = turn("strong")
    del state["model_route"]  # a thread checkpointed by a build that had no such key
    llm = ScriptedLLM(answer())

    await node(llm)(state, CONFIG)

    assert "model" not in llm.kwargs[0] and ledger_aliases(usage_event_sink) == [LOCAL]


@pytest.mark.parametrize("tier", ROUTED)
async def test_a_routed_turn_with_the_alias_since_removed_runs_local(priced, monkeypatch, usage_event_sink, tier):
    # the thread paused with this tier, then the operator unset its alias before the resume: there is nothing to call
    monkeypatch.setattr(model_route, "MEDIUM_CHAT_MODEL", "" if tier == "medium" else MEDIUM)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", "" if tier == "strong" else STRONG)
    llm = ScriptedLLM(answer())

    await node(llm)(turn(tier), CONFIG)

    assert "model" not in llm.kwargs[0] and ledger_aliases(usage_event_sink) == [LOCAL]


async def test_metered_invoke_adds_a_model_keyword_only_when_asked(priced):
    plain, overridden = ScriptedLLM(answer()), ScriptedLLM(answer())

    await metering.metered_invoke(plain, [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias=LOCAL)
    await metering.metered_invoke(overridden, [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias=STRONG, invoke_model=STRONG)

    assert plain.kwargs[0] == gateway.identity_from_config(CONFIG)
    assert overridden.kwargs[0] == {"model": STRONG, **gateway.identity_from_config(CONFIG)}


# ---- the routed alias is not there: one call, repeated locally -----------------------------------------------------------------
UNREACHABLE = [
    ("a refused connection", lambda: openai.APIConnectionError(request=_request())),
    ("a timeout", lambda: openai.APITimeoutError(request=_request())),
    ("a 500 as a real LiteLLM reports an unreachable upstream", lambda: status_error(500, _LITELLM_DEAD_UPSTREAM)),
    ("a 502", lambda: status_error(502)),
    ("a 503", lambda: status_error(503)),
    ("a 504", lambda: status_error(504)),
]


@pytest.mark.parametrize("tier", ROUTED)
@pytest.mark.parametrize("make_error", [make for _, make in UNREACHABLE], ids=[name for name, _ in UNREACHABLE])
async def test_a_routed_call_that_never_reached_a_model_is_repeated_once_on_the_local_alias(priced, routed, usage_event_sink, make_error, tier):
    llm = ScriptedLLM(make_error(), answer("the local model answered"))

    result = await node(llm)(turn(tier), CONFIG)

    assert [k.get("model") for k in llm.kwargs] == [ALIAS[tier], None]  # the routed alias first, then the plain local call
    assert llm.messages[0] == llm.messages[1]  # the very same request, not a rebuilt one
    assert result["messages"][0].content == "the local model answered"
    assert priced == [LOCAL] and ledger_aliases(usage_event_sink) == [LOCAL]  # the failed call recorded nothing; the answer is the local model's
    assert result["total_cost_usd"] == PRICES[LOCAL]
    assert routed.labels_seen == ["routed_unreachable_fell_back"]


@pytest.mark.parametrize(
    "error",
    [
        status_error(400, {"error": {"message": "bad request"}}),
        status_error(401),
        status_error(404),
        status_error(408),
        status_error(429),
        ValueError("a bug, not an outage"),
    ],
    ids=["400", "401", "404", "408", "429", "ValueError"],
)
@pytest.mark.parametrize("tier", ROUTED)
async def test_an_error_that_is_not_unreachability_is_not_swallowed(priced, routed, usage_event_sink, error, tier):
    llm = ScriptedLLM(error, answer())

    with pytest.raises(type(error)):
        await node(llm)(turn(tier), CONFIG)

    assert len(llm.kwargs) == 1 and routed.labels_seen == [] and ledger_aliases(usage_event_sink) == []


@pytest.mark.parametrize("tier", ROUTED)
async def test_the_gateways_budget_stop_is_never_papered_over_even_as_a_5xx(priced, routed, tier):
    # a spent budget repeats until it resets and is the operator's signal; a silent fallback would hide it
    budget = status_error(503, {"type": "budget_exceeded", "message": "Budget has been exceeded"})
    assert FAILURES.is_budget_exceeded(budget), "test setup: this error must be recognised as the budget stop"
    llm = ScriptedLLM(budget, answer())

    with pytest.raises(openai.APIStatusError):
        await node(llm)(turn(tier), CONFIG)

    assert len(llm.kwargs) == 1 and routed.labels_seen == []


async def test_if_the_local_repeat_also_fails_that_error_surfaces(priced, routed):
    llm = ScriptedLLM(status_error(503), openai.APIConnectionError(request=_request()))

    with pytest.raises(openai.APIConnectionError):
        await node(llm)(turn("strong"), CONFIG)

    assert routed.labels_seen == ["routed_unreachable_fell_back"]


async def test_the_next_call_in_the_same_turn_tries_the_routed_alias_again(priced, routed, usage_event_sink):
    llm = ScriptedLLM(status_error(503), answer(), answer())
    agent = node(llm)

    await agent(turn("medium"), CONFIG)
    await agent(turn("medium"), CONFIG)

    assert [k.get("model") for k in llm.kwargs] == [MEDIUM, None, MEDIUM]  # not remembered as down: each failure is counted on its own
    assert ledger_aliases(usage_event_sink) == [LOCAL, MEDIUM]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APIConnectionError(request=_request()), True),
        (openai.APITimeoutError(request=_request()), True),
        (status_error(502), True),
        (status_error(503), True),
        (status_error(504), True),
        (status_error(500), True),
        (status_error(500, _LITELLM_DEAD_UPSTREAM), True),
        (status_error(599), True),
        (status_error(400), False),
        (status_error(404), False),
        (status_error(408), False),
        (status_error(429), False),
        (status_error(503, {"type": "budget_exceeded"}), False),
        (RuntimeError("x"), False),
    ],
)
def test_routed_unreachable_is_connection_failures_timeouts_and_any_5xx_but_never_a_4xx_or_the_budget_stop(error, expected):
    assert routed_unreachable(error) is expected


def test_only_the_agent_node_asks_for_a_different_model():
    """Compaction and follow-ups are not the answer and keep the local alias (graph-seam.md). By construction: nothing but the agent node
    passes `invoke_model`, so no other call site can ever be sent to a routed alias."""
    root = Path(__file__).resolve().parents[3] / "app"
    users = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(isinstance(n, ast.keyword) and n.arg == "invoke_model" for n in ast.walk(ast.parse(path.read_text())))
    )

    assert users == ["agent/graph/agent_node.py"]


# ---- the real client: the claim a fake cannot settle ---------------------------------------------------------------------------
def _completion(model: str) -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "a sufficiently long answer to count"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@tool
def lookup(query: str) -> str:
    """Look something up."""
    return query


def real_client(requests: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=_completion(body["model"]))

    return ChatOpenAI(
        model=LOCAL, base_url="http://gateway.test/v1", api_key="test-key", max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ).bind_tools([lookup])  # the same shape make_llm builds: a ChatOpenAI bound to the domain's tools


async def test_the_real_chat_client_sends_the_routed_alias_for_a_routed_turn_and_chat_otherwise(priced, routed):
    requests: list[dict] = []
    agent = node(real_client(requests))

    await agent(turn("strong"), CONFIG)
    await agent(turn("medium"), CONFIG)
    await agent(turn("local"), CONFIG)

    assert [r["model"] for r in requests] == [STRONG, MEDIUM, LOCAL]  # the per-call keyword overrides the client's default model
    assert all(r["tools"] for r in requests) and requests[0]["messages"] == requests[1]["messages"] == requests[2]["messages"]


async def test_the_system_prompt_and_the_whole_request_are_identical_under_all_three_routes(priced, routed):
    seen: list = []
    llm = queue_llm(answer(), answer(), answer(), seen=seen)
    agent = make_agent_node(llm, model_alias=LOCAL, system_prompt="THE SYSTEM PROMPT")

    for route in ("strong", "medium", "local"):
        await agent(turn(route, context="some retrieved text"), CONFIG)

    assert [m.content for m in seen[0]] == [m.content for m in seen[1]] == [m.content for m in seen[2]]
    assert seen[0][0].content == "THE SYSTEM PROMPT"  # the router cannot vary the prompt the model is given


# ---- the guarantees that must not move -----------------------------------------------------------------------------------------
async def _mutating_turn(monkeypatch, label: str):
    """The same `add_note` turn, from the user's message to the approved write, with the router sending it where the label says."""
    from app.domains.ecorp import store as ecorp_store
    from app.retrieval import qdrant_store

    writes: list = []
    idempotency_keys: list = []

    async def fake_embed(text):
        return [0.0]

    async def fake_upsert(points):
        writes.append([(p.id, p.payload) for p in points])

    async def spying_idempotent(**kwargs):
        idempotency_keys.append((kwargs["tool_call_id"], kwargs["ctx"]["tenant"], kwargs["ctx"]["principal"], kwargs["tool_name"]))
        return await REAL_IDEMPOTENT(**kwargs)

    monkeypatch.setattr(ecorp_store, "embed_document", fake_embed)
    monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)
    monkeypatch.setattr(ecorp_tools, "idempotent", spying_idempotent)
    monkeypatch.setattr(model_route, "ROUTER_MODE", "on")
    monkeypatch.setattr(model_route, "MEDIUM_CHAT_MODEL", MEDIUM)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", STRONG)

    async def classify(question, text):
        return Labelled("labelled", label, label, 0.9, 0.1, "primary", "v1")

    use_classifier(monkeypatch, classify)
    kwargs_seen: list = []
    llm = queue_llm(call("add_note", {"title": "Refunds", "content": "30 days.", "topic": "company"}, "call_1"),
                    AIMessage(content="I've added a note about the refund policy."), kwargs_seen=kwargs_seen)
    graph = build_graph(GraphDeps(llm=llm))
    config = cfg("thread-1")

    await graph.ainvoke({"messages": [HumanMessage(content="remember our refund policy")]}, config=config)
    paused = await graph.aget_state(config)
    await graph.ainvoke(Command(resume=True), config=config)
    return {"paused_at": paused.next, "route": paused.values["model_route"], "writes": writes, "keys": idempotency_keys,
            "models": [k.get("model") for k in kwargs_seen]}


async def test_a_mutating_tool_call_gets_the_same_approval_scoping_and_idempotency_key_under_every_tier(monkeypatch, priced, routed):
    local = await _mutating_turn(monkeypatch, "lookup")
    medium = await _mutating_turn(monkeypatch, "analysis")
    strong = await _mutating_turn(monkeypatch, "reasoning")

    assert (local["route"], medium["route"], strong["route"]) == ("local", "medium", "strong")
    assert local["models"] == [None, None] and medium["models"] == [MEDIUM, MEDIUM] and strong["models"] == [STRONG, STRONG]  # the routes really differed ...
    assert local["paused_at"] and local["paused_at"] == medium["paused_at"] == strong["paused_at"]  # ... and all paused for the same human approval
    expected_key = [("call_1", TEST_CTX["tenant"], TEST_CTX["principal"], "add_note")]
    assert local["keys"] == medium["keys"] == strong["keys"] == expected_key
    assert local["writes"] == medium["writes"] == strong["writes"] and len(strong["writes"]) == 1  # one write, the same tenant-scoped payload


@pytest.mark.parametrize(("unpriced", "label", "expected_model", "expected_route"), [
    ({STRONG}, "reasoning", MEDIUM, "medium"),  # the strong alias has no price: the turn steps down to the medium one
    ({STRONG, MEDIUM}, "reasoning", None, "local"),  # neither does: the turn stays on the default model
    ({MEDIUM}, "analysis", None, "local"),
])
async def test_an_unpriced_alias_under_the_block_policy_steps_the_turn_down_and_never_refuses_it(monkeypatch, usage_event_sink, unpriced, label, expected_model, expected_route):
    async def get_price(alias):
        return None if alias in unpriced else object()

    monkeypatch.setattr(pricing, "UNPRICED_MODEL_POLICY", "block")
    monkeypatch.setattr(pricing, "get_price", get_price)

    async def classify(question, text):
        return Labelled("labelled", label, label, 0.9, 0.1, "primary", "v1")

    use_classifier(monkeypatch, classify)
    monkeypatch.setattr(model_route, "ROUTER_MODE", "on")
    monkeypatch.setattr(model_route, "MEDIUM_CHAT_MODEL", MEDIUM)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", STRONG)
    kwargs_seen: list = []
    graph = build_graph(GraphDeps(llm=queue_llm(AIMessage(content="A long enough answer."), kwargs_seen=kwargs_seen)))
    config = cfg()

    result = await graph.ainvoke({"messages": [HumanMessage(content="a question")]}, config=config)

    assert result["messages"][-1].content == "A long enough answer."  # the turn was answered, not refused
    assert (await graph.aget_state(config)).values["model_route"] == expected_route
    assert [k.get("model") for k in kwargs_seen] == [expected_model]


async def test_a_shadow_router_changes_no_gateway_call_and_no_ledger_row_compared_with_an_off_one(monkeypatch, priced, usage_event_sink):
    """Contract invariant 2. `shadow` decides "strong" here (and the decision is counted), yet the turn's calls and its ledger rows are exactly
    those of a router that is off: that is what makes it safe to leave on while an operator reads what routing would have cost."""
    tiers: list[tuple[str, str, str]] = []

    class _Tiers:
        def labels(self, **kw):
            tiers.append((kw["tier"], kw["mode"], kw["decided_by"]))
            return type("_Bound", (), {"inc": staticmethod(lambda amount=1: None)})()

    async def classify(question, text):
        return Labelled("labelled", "reasoning", "reasoning", 0.9, 0.1, "primary", "v1")

    use_classifier(monkeypatch, classify)
    monkeypatch.setattr(model_route.metrics, "agent_model_route_tier_total", _Tiers())
    monkeypatch.setattr(model_route, "MEDIUM_CHAT_MODEL", MEDIUM)
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", STRONG)

    def without_session(kwargs):  # a fresh session id is minted per turn; it is not what is being compared
        body = kwargs.get("extra_body", {})
        return {**kwargs, "extra_body": {**body, "litellm_session_id": None, "metadata": {**body.get("metadata", {}), "session_id": None}}}

    async def one_turn(mode):
        monkeypatch.setattr(model_route, "ROUTER_MODE", mode)
        kwargs_seen: list = []
        graph = build_graph(GraphDeps(llm=queue_llm(AIMessage(content="A long enough answer."), kwargs_seen=kwargs_seen)))
        config = cfg()
        before = len(usage_event_sink)
        result = await graph.ainvoke({"messages": [HumanMessage(content="a question")]}, config=config)
        rows = [{k: row[k] for k in ("model_alias", "kind")} for row in usage_event_sink[before:]]
        return [without_session(k) for k in kwargs_seen], rows, (await graph.aget_state(config)).values["model_route"], result["messages"][-1].content

    off = await one_turn("off")
    shadow = await one_turn("shadow")

    assert shadow[0] == off[0] and shadow[1] == off[1] and shadow[3] == off[3]  # the same gateway keywords, ledger rows and answer
    assert [k.get("model") for k in shadow[0]] == [None] and shadow[2] == off[2] == "local"
    assert tiers == [("strong", "shadow", "label")]  # ... while the decision it WOULD have made was counted, and the off router counted none
