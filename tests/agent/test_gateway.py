"""The app <-> gateway contract (app/agent/gateway.py): who each call is attributed to, and what the
app does when the gateway says its key is out of budget.

Two halves, both about the gateway being the BACKSTOP behind the app-level ceilings:

  * Identity. The gateway's spend logs and Langfuse show one anonymous caller unless every call
    names the tenant. The wire test below drives a real `ChatOpenAI` into an `httpx.MockTransport`
    so it pins what actually leaves the process, not what we believe langchain forwards — a
    kwarg langchain silently dropped would otherwise pass every unit test here.
  * Stop. LiteLLM answers a key past its `max_budget` with HTTP 400 `type: budget_exceeded`. That
    is a real `openai.BadRequestError` out of the agent node; before this it surfaced as an
    anonymous `internal` error, so nobody learned the backstop had fired.
"""
import hashlib
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agent import gateway
from app.agent import runtime as runtime_module
from app.agent import runtime_legacy_stream as legacy_stream_module
from app.agent import runtime_stream as stream_module
from app.agent.graph import AGENT_RETRY_POLICY, GraphDeps
from app.agent.graph_agent_node import make_agent_node
from app.agent.graph_build import build_graph
from app.agent.graph_compaction import _estimate_tokens, make_compact_history_node
from app.agent.graph_followups import make_suggest_followups_node
from app.core import metrics, tracing
from app.core.errors import ErrorCode
from tests.agent.test_graph_integration import _config
from tests.conftest import TEST_CTX, metric_value

# What LiteLLM really sends for a key past its budget. Captured from ghcr.io/berriai/litellm:main-stable
# (2026-10): HTTP 429, body as below. The type is the same on older LiteLLM code paths that answered 400,
# so the tests cover both statuses; the openai SDK turns the body's `error` into `.type`/`.code`.
LITELLM_BUDGET_BODY = {
    "error": {
        "message": "Budget has been exceeded! Key=agent-core-app (sk-...SHBA) Current cost: 612.4, Max budget: 600.0",
        "type": "budget_exceeded",
        "param": None,
        "code": "429",
    }
}


def _status_error(status: int, body: dict) -> openai.APIStatusError:
    """A real SDK exception, of the class the SDK picks for that status (not a lookalike)."""
    request = httpx.Request("POST", "http://litellm:4000/v1/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    error_class = {
        400: openai.BadRequestError,
        429: openai.RateLimitError,
    }.get(status, openai.APIStatusError)
    return error_class(body["error"]["message"], response=response, body=body["error"])


def _budget_error(status: int = 429) -> openai.APIStatusError:
    return _status_error(status, LITELLM_BUDGET_BODY)


def _count() -> float:
    return metric_value(metrics.agent_gateway_budget_exceeded_total)


class TestEndUserId:
    def test_it_is_stable_so_a_tenants_calls_group_together(self):
        assert gateway.end_user_id("acme") == gateway.end_user_id("acme")

    def test_it_differs_per_tenant(self):
        assert gateway.end_user_id("acme") != gateway.end_user_id("globex")

    def test_it_never_contains_the_tenant_name(self):
        """LiteLLM forwards `user` to the model provider; a customer's name must not leave."""
        tenant = "very-secret-customer-name"
        end_user = gateway.end_user_id(tenant)

        assert tenant not in end_user
        assert end_user == "tenant_" + hashlib.sha256(tenant.encode()).hexdigest()[:16]


class TestCallIdentity:
    def test_a_valid_ctx_names_the_tenant_to_the_gateway(self):
        identity = gateway.call_identity({"tenant": "acme", "principal": "alice", "claims": {}})

        assert identity["user"] == gateway.end_user_id("acme")
        assert identity["extra_body"]["metadata"] == {
            "tenant": "acme",
            "principal": "alice",
            "tags": ["tenant:acme"],
        }

    def test_the_provider_facing_field_carries_no_readable_name(self):
        """`user` goes to the provider; only `metadata` (gateway-only) may be readable."""
        identity = gateway.call_identity({"tenant": "acme", "principal": "alice", "claims": {}})

        assert "acme" not in identity["user"] and "alice" not in identity["user"]

    @pytest.mark.parametrize("ctx", [None, {}, {"tenant": "", "principal": "alice"}, {"tenant": "acme"}])
    def test_an_invalid_ctx_sends_the_call_as_it_always_was(self, ctx):
        """Never invent an identity: an unattributable call carries none rather than a wrong one."""
        assert gateway.call_identity(ctx) == {}


class TestWhatActuallyLeavesTheProcess:
    """Drive a real ChatOpenAI at a mock gateway and read the request it sent."""

    @staticmethod
    def _llm(handler) -> ChatOpenAI:
        return ChatOpenAI(
            model="chat",
            api_key="sk-app-key",
            base_url="http://litellm:4000/v1",
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )

    @staticmethod
    def _completion() -> dict:
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    async def test_the_request_body_carries_user_and_metadata(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=self._completion())

        await self._llm(handler).ainvoke([HumanMessage(content="hi")], **gateway.call_identity(TEST_CTX))

        body = seen[0]
        assert body["user"] == gateway.end_user_id(TEST_CTX["tenant"])
        assert body["metadata"] == {
            "tenant": TEST_CTX["tenant"],
            "principal": TEST_CTX["principal"],
            "tags": [f"tenant:{TEST_CTX['tenant']}"],
        }

    async def test_without_an_identity_the_body_is_what_it_was_before(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=self._completion())

        await self._llm(handler).ainvoke([HumanMessage(content="hi")])

        assert "user" not in seen[0] and "metadata" not in seen[0]

    @pytest.mark.parametrize("status", [429, 400])
    async def test_litellms_budget_stop_arrives_as_something_we_recognise(self, status):
        """The classification must hold for the exception the SDK really raises from LiteLLM's
        response, not only for the hand-built one in the unit tests below."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=LITELLM_BUDGET_BODY)

        with pytest.raises(openai.APIStatusError) as raised:
            await self._llm(handler).ainvoke([HumanMessage(content="hi")], **gateway.call_identity(TEST_CTX))

        assert raised.value.status_code == status
        assert gateway.is_budget_exceeded(raised.value)


class TestRecognisingTheGatewaySayingStop:
    @pytest.mark.parametrize("status", [429, 400])
    def test_a_budget_exceeded_error_is_recognised_at_either_status(self, status):
        assert gateway.is_budget_exceeded(_budget_error(status))

    def test_any_other_400_is_not(self):
        other = _status_error(400, {"error": {"message": "bad request", "type": "invalid_request_error"}})

        assert not gateway.is_budget_exceeded(other)

    def test_a_real_rate_limit_is_not(self):
        """A 429 of another type is the key's rpm/tpm limit: retryable, and a different fix from
        "out of budget" — which is why the status alone cannot decide this."""
        rate_limited = _status_error(429, {"error": {"message": "slow down", "type": "throttling_error"}})

        assert not gateway.is_budget_exceeded(rate_limited)

    def test_an_unrelated_exception_is_not_even_with_a_matching_attribute(self):
        """Only the SDK's status error counts; a stray `.type` on something else must not."""
        impostor = RuntimeError("budget_exceeded")
        impostor.type = "budget_exceeded"  # type: ignore[attr-defined] - the point of the test

        assert not gateway.is_budget_exceeded(impostor)

    def test_the_caller_facing_envelope_names_no_money_and_no_key(self):
        envelope = gateway.budget_envelope()
        text = json.dumps(envelope.to_dict())

        assert envelope.code is ErrorCode.PROVIDER_BUDGET_EXCEEDED
        assert "$" not in text and "sk-" not in text and "600" not in text
        assert envelope.message


class _RecordingLLM:
    """Remembers the keyword arguments each `ainvoke` got, which is what carries the identity."""

    def __init__(self, *responses):
        self._inner = GenericFakeChatModel(messages=iter(responses))
        self.kwargs: list[dict] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.kwargs.append(kwargs)
        return await self._inner.ainvoke(messages)


class TestTheAgentNodeAttributesItsCalls:
    async def test_a_call_made_under_a_ctx_carries_the_tenants_identity(self):
        llm = _RecordingLLM(AIMessage(content="hello there"))
        agent = make_agent_node(llm)

        await agent({"messages": [HumanMessage(content="hi")]}, {"configurable": {"ctx": TEST_CTX}})

        assert llm.kwargs == [gateway.call_identity(TEST_CTX)]
        assert llm.kwargs[0]["user"] == gateway.end_user_id(TEST_CTX["tenant"])

    async def test_a_direct_call_with_no_config_still_works_and_sends_no_identity(self):
        """Tests and scripts call the node with just a state."""
        llm = _RecordingLLM(AIMessage(content="hello there"))
        agent = make_agent_node(llm)

        await agent({"messages": [HumanMessage(content="hi")]})

        assert llm.kwargs == [{}]

    async def test_a_real_graph_run_hands_the_node_its_ctx(self):
        """Through `build_graph`, not around it: LangGraph decides whether a node receives `config`
        from its signature, and the `_instrumented` wrapper sits between the two. If the ctx did not
        arrive, every production call would silently be anonymous and no test above would notice."""
        llm = _RecordingLLM(AIMessage(content="This is a sufficiently long final answer."))
        graph = build_graph(GraphDeps(llm=llm))

        await graph.ainvoke({"messages": [HumanMessage(content="what is a checkpointer?")]}, config=_config())

        assert llm.kwargs, "the graph never reached the LLM"
        assert all(call.get("user") == gateway.end_user_id(TEST_CTX["tenant"]) for call in llm.kwargs)


class _RaisingLLM:
    """An LLM whose every call is refused, recording what it was sent."""

    def __init__(self, exc):
        self._exc = exc
        self.kwargs: list[dict] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.kwargs.append(kwargs)
        raise self._exc


_CONFIG = {"configurable": {"ctx": TEST_CTX}}


def _followup_state() -> dict:
    return {
        "messages": [AIMessage(content="A grounded answer about checkpointers.")],
        "used_citations": ["doc-1"],
    }


def _history(turns: int = 3) -> list:
    messages = [SystemMessage(content="seed", id="sys")]
    for i in range(turns):
        messages.append(HumanMessage(content=f"question number {i} with some real words", id=f"h{i}"))
        messages.append(AIMessage(content=f"answer number {i} with some real words too", id=f"a{i}"))
    return messages


def _tripped_ceiling(messages) -> int:
    return _estimate_tokens([m for m in messages if not isinstance(m, SystemMessage)]) - 1


class TestEveryNodeThatSpendsOnTheTenantsBehalfIsAttributed:
    """The agent node is the big spender, but follow-ups and history compaction call the same
    model on the same tenant's turn. Left anonymous they would be the part of the bill that
    gateway-side attribution cannot explain."""

    async def test_follow_up_suggestions_carry_the_identity(self):
        llm = _RecordingLLM(AIMessage(content="What next?\nAnd then?"))

        await make_suggest_followups_node(llm)(_followup_state(), _CONFIG)

        assert llm.kwargs == [gateway.call_identity(TEST_CTX)]

    async def test_history_compaction_carries_the_identity(self):
        llm = _RecordingLLM(AIMessage(content="a summary"))
        messages = _history()

        await make_compact_history_node(llm, ceiling=_tripped_ceiling(messages), floor=1)(
            {"messages": messages}, _CONFIG
        )

        assert llm.kwargs == [gateway.call_identity(TEST_CTX)]


class TestASwallowedBudgetStopIsStillCounted:
    """Follow-ups and compaction degrade quietly by design (the answer is already good, the trim
    does not need a summary). That must not hide the backstop firing: a log line is not an alert."""

    async def test_follow_ups_degrade_and_the_stop_is_counted(self):
        before = _count()

        result = await make_suggest_followups_node(_RaisingLLM(_budget_error()))(_followup_state(), _CONFIG)

        assert result == {"followups": []}
        assert _count() == before + 1

    async def test_compaction_degrades_and_the_stop_is_counted(self):
        before = _count()
        messages = _history()
        compact = make_compact_history_node(
            _RaisingLLM(_budget_error()), ceiling=_tripped_ceiling(messages), floor=1
        )

        result = await compact({"messages": messages}, _CONFIG)

        assert "messages" in result, "the trim itself must still happen"
        assert "history_summary" not in result
        assert _count() == before + 1

    async def test_an_ordinary_failure_in_either_is_not_counted_as_the_backstop(self):
        before = _count()
        messages = _history()

        await make_suggest_followups_node(_RaisingLLM(RuntimeError("model unreachable")))(
            _followup_state(), _CONFIG
        )
        await make_compact_history_node(
            _RaisingLLM(RuntimeError("model unreachable")), ceiling=_tripped_ceiling(messages), floor=1
        )({"messages": messages}, _CONFIG)

        assert _count() == before


class TestABudgetStopIsNotRetried:
    """A budget stop repeats until the budget resets, so retrying it cannot succeed. LangGraph's
    default `retry_on` retries anything that is not a programming error, which includes the SDK's
    status errors, so the agent node retried the refusal 3 times — each attempt (and the SDK's own
    2 retries inside it, measured against a real LiteLLM: 3 requests per `ainvoke`) another refused
    call in the gateway's log, and seconds of backoff before the caller learned anything."""

    async def test_the_agent_node_gives_up_on_the_first_refusal(self):
        llm = _RaisingLLM(_budget_error())
        graph = build_graph(GraphDeps(llm=llm))

        with pytest.raises(openai.APIStatusError):
            await graph.ainvoke({"messages": [HumanMessage(content="what is a checkpointer?")]}, config=_config())

        assert len(llm.kwargs) == 1

    def test_the_policy_still_retries_what_it_always_did(self):
        """Only the budget stop is carved out: a dropped connection or a 5xx is still worth another go,
        and a programming error is still not."""
        retry_on = AGENT_RETRY_POLICY.retry_on

        assert retry_on(ConnectionError("reset")) is True
        assert retry_on(_status_error(503, {"error": {"message": "overloaded", "type": "server_error"}})) is True
        assert retry_on(_status_error(429, {"error": {"message": "slow down", "type": "throttling_error"}})) is True
        assert retry_on(RuntimeError("a bug")) is False

    def test_the_budget_stop_is_refused_a_retry_whatever_status_the_gateway_used(self):
        """Verified against LiteLLM main-stable: 429. Older code paths answer 400. The type decides."""
        retry_on = AGENT_RETRY_POLICY.retry_on

        assert retry_on(_budget_error(400)) is False
        assert retry_on(_budget_error(429)) is False


class _RaisingGraph:
    """Raises as soon as it is iterated: what a gateway refusal looks like inside a turn."""

    def __init__(self, exc):
        self._exc = exc

    async def astream_events(self, graph_input, config=None, version="v2"):
        raise self._exc
        yield  # pragma: no cover - makes this an async generator

    async def aget_state(self, cfg):
        return SimpleNamespace(values={}, next=(), tasks=[])


async def _run_stream(exc) -> list[dict]:
    cfg = {"configurable": {"thread_id": "fake-thread", "ctx": TEST_CTX}}
    return [event async for event in stream_module._run_graph_stream(_RaisingGraph(exc), {}, cfg, trace=None)]


class TestTheStreamReportsTheBackstopFiring:
    async def test_a_gateway_budget_stop_is_its_own_error_not_an_anonymous_internal_one(self):
        events = await _run_stream(_budget_error())

        assert [e["type"] for e in events] == ["error"]
        assert events[0]["code"] == "provider_budget_exceeded"

    async def test_it_does_not_leak_the_gateways_message(self):
        """LiteLLM's message states the key's spend and its limit."""
        events = await _run_stream(_budget_error())

        assert "612" not in json.dumps(events[0]) and "600" not in json.dumps(events[0])
        assert "agent-core-app" not in json.dumps(events[0])

    async def test_it_is_counted_so_the_alert_can_fire(self):
        before = _count()

        await _run_stream(_budget_error())

        assert _count() == before + 1

    async def test_another_gateway_400_stays_an_internal_error_and_is_not_counted(self):
        before = _count()

        events = await _run_stream(_status_error(400, {"error": {"message": "bad", "type": "invalid_request_error"}}))

        assert events[0]["code"] == "internal"
        assert _count() == before


class TestTheLegacyStreamAgrees:
    """`astream_events_turn_ctx` is not wired into a production caller (its module says so) but it
    shares the contract, and a copy of the error handling that quietly differs is how the two drift."""

    @pytest.fixture
    def drive(self, monkeypatch):
        async def init_graph_async():
            return _RaisingGraph(self.exc)

        async def ensure_seeded(graph, thread_id):
            return None

        monkeypatch.setattr(runtime_module, "init_graph_async", init_graph_async)
        monkeypatch.setattr(runtime_module, "_ensure_seeded_async", ensure_seeded)
        monkeypatch.setattr(tracing, "get_langfuse", lambda: None)

        async def run(exc) -> list[dict]:
            self.exc = exc
            return [e async for e in legacy_stream_module.astream_events_turn_ctx("hi", "thread-1", TEST_CTX)]

        return run

    async def test_a_gateway_budget_stop_is_reported_and_counted(self, drive):
        before = _count()

        events = await drive(_budget_error())

        assert [e["type"] for e in events] == ["error"]
        assert events[0]["code"] == "provider_budget_exceeded"
        assert _count() == before + 1

    async def test_an_ordinary_failure_is_still_internal(self, drive):
        events = await drive(RuntimeError("boom"))

        assert events[0]["code"] == "internal"
