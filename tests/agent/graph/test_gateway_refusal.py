"""Regression tests for what Langfuse traces `b376e4d8` and `2c8e76a4` (2026-10-21) showed.

A gateway content guardrail ("Denied Financial Advice", set up in LiteLLM's admin UI) refused
"should i invest in testla stock ?" with an HTTP 400. Three things then went wrong in the app:

1. The `agent` node retried the refusal three times (~5 s of backoff, three more refused calls in
   the gateway's log): LangGraph's default `retry_on` excludes `httpx` 4xx errors, but the OpenAI
   SDK's status errors are not `httpx.HTTPStatusError`, so EVERY 4xx counted as transient.
2. The person got an anonymous `internal` error for what was a policy decision.
3. The refused message stayed in the checkpointed thread. LiteLLM scans the WHOLE message list, so the
   next turn ("hey") was refused for the earlier message ("stock + should i"), and so was every turn
   after it: one refusal ended the session.

A refusal is now final (not retried), is the turn's answer, and the message it refused is replaced in
the thread. Also here: the model calls that billed output tokens and returned nothing at all, which
is how Ollama's dropped `remember` tool calls reached the graph, are counted instead of silent.
"""
import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from app.agent.graph.build import GraphDeps
from app.agent.graph.core import AGENT_RETRY_POLICY
from app.agent.graph.messages import CANNOT_HELP_MESSAGE, GATEWAY_REFUSAL_KEY
from app.agent.runtime import stream as stream_module
from app.core import gateway, metrics
from tests.agent.fakes import cfg, make_agent_node, scripted_llm
from tests.conftest import metric_value
from tests.ecorp import build_graph

# What LiteLLM really sent in the trace: the guardrail refusal, body verbatim (`type` and `param` are
# the STRING "None").
_MESSAGE = "Content blocked: denied_financial_advice conditional match 'stock + should i' detected (severity: high)"


def _guardrail_body(mode: str = "pre_call") -> dict:
    return {
        "error": {
            "message": _MESSAGE,
            "type": "None",
            "param": "None",
            "code": "400",
            "provider_specific_fields": {
                "error": _MESSAGE,
                "category": "denied_financial_advice",
                "matched_phrase": "stock + should i",
                "severity": "high",
                "guardrail_name": "Denied Financial Advice",
                "guardrail_mode": mode,
            },
        }
    }


def _status_error(status: int, body: dict) -> openai.APIStatusError:
    """A real SDK exception of the class the SDK picks for that status."""
    request = httpx.Request("POST", "http://litellm:4000/v1/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    cls = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        404: openai.NotFoundError,
        422: openai.UnprocessableEntityError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return cls(body["error"]["message"], response=response, body=body["error"])


def _guardrail_error(mode: str = "pre_call") -> openai.BadRequestError:
    return _status_error(400, _guardrail_body(mode))  # type: ignore[return-value]  # status 400 is always a BadRequestError


def _blocked(mode: str) -> float:
    return metric_value(metrics.agent_gateway_content_blocked_total, mode=mode)


class TestRecognisingAContentBlock:
    async def test_the_guardrail_refusal_arrives_as_something_we_recognise(self):
        """Through the real SDK and a real ChatOpenAI, not only the hand-built exception: the shape
        of `.body` is the SDK's to decide."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=_guardrail_body())

        llm = ChatOpenAI(
            model="chat",
            api_key="sk-app-key",
            base_url="http://litellm:4000/v1",
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )
        with pytest.raises(openai.BadRequestError) as raised:
            await llm.ainvoke([HumanMessage(content="should i invest in testla stock ?")])

        block = gateway.content_block(raised.value)
        assert block is not None and block.mode == "pre_call"

    @pytest.mark.parametrize("mode", ["pre_call", "post_call", "during_call"])
    def test_the_mode_is_read_from_the_guardrail(self, mode):
        assert gateway.content_block(_guardrail_error(mode)).mode == mode

    def test_a_mode_it_does_not_know_is_counted_as_other_so_the_label_stays_bounded(self):
        assert gateway.content_block(_guardrail_error("some-new-mode")).mode == "other"

    def test_a_block_without_the_structured_fields_is_still_recognised_by_its_message(self):
        error = _status_error(400, {"error": {"message": _MESSAGE, "type": "None"}})
        assert gateway.content_block(error).mode == "other"

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                _status_error(400, {"error": {"message": "context length exceeded", "type": "invalid_request_error"}}),
                id="another 400 is a bug, not a refusal",
            ),
            pytest.param(
                _status_error(429, {"error": {"message": "Budget has been exceeded!", "type": "budget_exceeded"}}),
                id="the budget stop has its own handling",
            ),
            pytest.param(_status_error(500, {"error": {"message": "boom", "type": "server_error"}}), id="a 5xx"),
            pytest.param(RuntimeError(_MESSAGE), id="a lookalike message on another exception"),
        ],
    )
    def test_nothing_else_is_taken_for_a_content_block(self, exc):
        assert gateway.content_block(exc) is None


class TestPermanentClientErrors:
    @pytest.mark.parametrize("status", [400, 401, 404, 422])
    def test_a_rejection_that_repeating_cannot_change_is_permanent(self, status):
        assert gateway.is_permanent_client_error(_status_error(status, {"error": {"message": "no", "type": "x"}}))

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_a_rate_limit_or_a_server_error_is_not(self, status):
        assert not gateway.is_permanent_client_error(_status_error(status, {"error": {"message": "slow", "type": "x"}}))

    @pytest.mark.parametrize("status", [408, 409, 425])
    def test_a_timeout_or_conflict_is_worth_another_go(self, status):
        error = openai.APIStatusError(
            "later",
            response=httpx.Response(status, request=httpx.Request("POST", "http://x")),
            body={"message": "later"},
        )
        assert not gateway.is_permanent_client_error(error)

    def test_an_exception_that_is_not_a_status_error_is_not(self):
        assert not gateway.is_permanent_client_error(ConnectionError("reset"))
        assert not gateway.is_permanent_client_error(RuntimeError("a bug"))


class _RaisingLLM:
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        raise self._exc


class TestARefusalIsNotRetried:
    @pytest.mark.parametrize("status", [400, 401, 404, 422])
    def test_the_policy_gives_up_on_a_permanent_client_error(self, status):
        error = _status_error(status, {"error": {"message": "no", "type": "x"}})
        assert AGENT_RETRY_POLICY.retry_on(error) is False

    def test_it_still_retries_what_it_always_did(self):
        retry_on = AGENT_RETRY_POLICY.retry_on
        assert retry_on(ConnectionError("reset")) is True
        assert retry_on(_status_error(503, {"error": {"message": "overloaded", "type": "server_error"}})) is True
        assert retry_on(_status_error(429, {"error": {"message": "slow down", "type": "throttling_error"}})) is True
        assert retry_on(RuntimeError("a bug")) is False

    async def test_a_rejected_api_key_is_one_call_not_three(self):
        llm = _RaisingLLM(_status_error(401, {"error": {"message": "bad key", "type": "auth_error"}}))
        graph = build_graph(GraphDeps(llm=llm))

        with pytest.raises(openai.AuthenticationError):
            await graph.ainvoke({"messages": [HumanMessage(content="what is a checkpointer?")]}, config=cfg())

        assert llm.calls == 1

    async def test_a_400_that_is_not_a_guardrail_still_raises_for_the_stream_to_report(self):
        llm = _RaisingLLM(_status_error(400, {"error": {"message": "context length exceeded", "type": "invalid_request_error"}}))
        graph = build_graph(GraphDeps(llm=llm))

        with pytest.raises(openai.BadRequestError):
            await graph.ainvoke({"messages": [HumanMessage(content="what is a checkpointer?")]}, config=cfg())

        assert llm.calls == 1


ASKED = "should i invest in testla stock ?"


def _guarded_llm(seen: list, banned: str = "testla"):
    """A model behind a gateway that refuses a request when ANY message in it contains `banned`: what
    LiteLLM's content filter does (it scans the whole message list, not only the latest message)."""

    def respond(messages):
        if any(banned in str(m.content).lower() for m in messages):
            raise _guardrail_error()
        return AIMessage(content="Hello there, how can I help you today?")

    return scripted_llm(respond, seen)


class TestARefusalIsTheAnswer:
    async def test_the_person_gets_a_refusal_not_an_error(self):
        seen: list = []
        before = _blocked("pre_call")
        graph = build_graph(GraphDeps(llm=_guarded_llm(seen)))

        result = await graph.ainvoke({"messages": [HumanMessage(content=ASKED)]}, config=cfg())

        assert result["messages"][-1].content == CANNOT_HELP_MESSAGE
        assert result["messages"][-1].additional_kwargs[GATEWAY_REFUSAL_KEY] is True
        assert _blocked("pre_call") == before + 1

    async def test_what_the_person_sees_is_a_token_and_a_done_not_an_error_event(self):
        """The layer the web UI and the queue worker read: before the fix this stream ended in an
        `error` event carrying the generic internal-error message."""
        graph = build_graph(GraphDeps(llm=_guarded_llm([])))

        events = [
            event
            async for event in stream_module._run_graph_stream(
                graph, {"messages": [HumanMessage(content=ASKED)]}, cfg(), trace=None
            )
        ]

        assert [e["type"] for e in events] == ["token", "done"]
        assert events[0]["content"] == CANNOT_HELP_MESSAGE

    async def test_it_took_one_call_and_nothing_after_it(self):
        """No retry, and no follow-up suggestions or cache write: those make more model calls or
        store the refusal as 'the answer' to this question."""
        seen: list = []
        cache_writes: list = []

        async def cache_set(ctx, query, answer, citations):
            cache_writes.append(query)

        graph = build_graph(GraphDeps(llm=_guarded_llm(seen), cache_set=cache_set))
        await graph.ainvoke({"messages": [HumanMessage(content=ASKED)]}, config=cfg())

        assert len(seen) == 1
        assert cache_writes == []

    async def test_the_message_it_refused_is_not_left_in_the_thread(self):
        graph = build_graph(GraphDeps(llm=_guarded_llm([])))
        config = cfg()

        await graph.ainvoke({"messages": [HumanMessage(content=ASKED)]}, config=config)

        stored = (await graph.aget_state(config)).values["messages"]
        assert not any("testla" in str(m.content).lower() for m in stored)
        assert [type(m).__name__ for m in stored] == ["HumanMessage", "AIMessage"]

    async def test_the_next_turn_in_the_same_session_is_answered(self):
        """The trace: "hey" was refused for the earlier message's "stock + should i", and so was every
        turn after it. Before the fix this raises on the second turn."""
        seen: list = []
        graph = build_graph(GraphDeps(llm=_guarded_llm(seen)))
        config = cfg()

        await graph.ainvoke({"messages": [HumanMessage(content=ASKED)]}, config=config)
        result = await graph.ainvoke({"messages": [HumanMessage(content="hey")]}, config=config)

        assert result["messages"][-1].content == "Hello there, how can I help you today?"

    async def test_a_response_side_block_does_not_blame_the_persons_message(self):
        """A `post_call` guardrail refused what the MODEL wrote; the question is fine and stays."""

        def respond(messages):
            raise _guardrail_error("post_call")

        graph = build_graph(GraphDeps(llm=scripted_llm(respond)))
        config = cfg()
        before = _blocked("post_call")

        result = await graph.ainvoke({"messages": [HumanMessage(content="what is a checkpointer?")]}, config=config)

        assert result["messages"][-1].content == CANNOT_HELP_MESSAGE
        stored = (await graph.aget_state(config)).values["messages"]
        assert stored[0].content == "what is a checkpointer?"
        assert _blocked("post_call") == before + 1


class TestAnEmptyCompletionThatBilledTokens:
    """Ollama (qwen2.5:3b) generated 18-23 tokens for "remember this" and returned `content: ""` with no
    tool call: it had dropped a tool call it could not parse. To the graph that looked exactly like
    an empty reply, so nothing said the gateway had been billed for something we never saw."""

    @staticmethod
    async def _call(reply: AIMessage) -> None:
        agent = make_agent_node(scripted_llm(lambda _messages: reply))
        await agent({"messages": [HumanMessage(content="remember this")]})

    @staticmethod
    def _count() -> float:
        return metric_value(metrics.agent_unparsed_completion_total)

    async def test_output_tokens_with_no_text_and_no_tool_call_are_counted(self):
        before = self._count()
        await self._call(
            AIMessage(content="", usage_metadata={"input_tokens": 2787, "output_tokens": 19, "total_tokens": 2806})
        )
        assert self._count() == before + 1

    async def test_a_tool_call_is_not(self):
        before = self._count()
        await self._call(
            AIMessage(
                content="",
                tool_calls=[{"name": "remember", "args": {"content": "abc"}, "id": "c1"}],
                usage_metadata={"input_tokens": 10, "output_tokens": 19, "total_tokens": 29},
            )
        )
        assert self._count() == before

    async def test_text_is_not(self):
        before = self._count()
        await self._call(
            AIMessage(content="Hello!", usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13})
        )
        assert self._count() == before

    async def test_a_reply_that_billed_nothing_is_not(self):
        """No usage reported (a fake, or a proxy that omits it): not evidence of anything dropped."""
        before = self._count()
        await self._call(AIMessage(content=""))
        assert self._count() == before

