"""Per-call usage events (app/agent/cost/usage_events.py, app/agent/cost/metering.py).

Four things are proven here, each against the cheapest tier that can prove it:

  * the identity a billing meter hangs everything on, PINNED against the real libraries (a real
    `ChatOpenAI` into a mock gateway, through a real checkpointed graph), because the design relies
    on how langchain assigns a message id and that is exactly the kind of third-party behaviour that
    changes in an upgrade (constitution VII: verify, do not assume);
  * what a row contains and what a call without a tenant, usage or id does;
  * that every failure path is counted instead of failing the turn it records (principle V);
  * that the per-call events add up to the running total the per-turn ledger row is written from.

The primary key and the `ON CONFLICT` the duplicate story depends on are real-Postgres behaviour:
tests/integration/test_usage_events_real_postgres.py; the statement's text and parameters are the adapter's, in
tests/adapters/postgres/test_usage_events.py. Here the store is a fake (tests/fakes/cost.py).
"""
import uuid
from typing import Annotated, TypedDict

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.cost import metering, pricing, usage_events
from app.agent.graph.build import GraphDeps
from app.core import metrics
from app.ports import cost as cost_ports
from app.ports import models as model_ports
from app.ports.cost import UsageTableMissing
from tests.agent.fakes import make_agent_node
from tests.agent.graph.test_graph_integration import _config
from tests.conftest import TEST_CTX, metric_value
from tests.ecorp import build_graph
from tests.fakes.cost import InMemoryUsageEventStore

CONFIG = {"configurable": {"ctx": TEST_CTX, "thread_id": "thread-1"}}


def _usage(input_tokens=100, output_tokens=50, cached=0):
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached:
        usage["input_token_details"] = {"cache_read": cached}
    return usage


def _reply(text="an answer", **usage_kwargs):
    return AIMessage(content=text, usage_metadata=_usage(**usage_kwargs))


def _llm(*messages):
    return GenericFakeChatModel(messages=iter(messages))


def _count(path):
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


def _price_models(monkeypatch, **per_alias):
    """Make `pricing` know these aliases: {alias: (input_per_token, output_per_token)}."""

    async def fetch():
        return [
            {
                "model_name": alias,
                "model_info": {"input_cost_per_token": rates[0], "output_cost_per_token": rates[1]},
            }
            for alias, rates in per_alias.items()
        ]

    monkeypatch.setattr(pricing, "_fetch_model_info", fetch)
    pricing.reset_pricing_state()


class TestEventId:
    def test_it_is_a_pure_function_of_tenant_and_message(self):
        assert usage_events.event_id_for("acme", "run--1-0") == usage_events.event_id_for("acme", "run--1-0")

    def test_it_differs_per_message_and_per_tenant(self):
        base = usage_events.event_id_for("acme", "run--1-0")

        assert base != usage_events.event_id_for("acme", "run--2-0")
        assert base != usage_events.event_id_for("globex", "run--1-0")

    def test_it_fits_a_providers_idempotency_key(self):
        """Stripe's `identifier` is at most 100 characters."""
        event_id = usage_events.event_id_for("a-very-long-tenant-name" * 20, "run--" + "x" * 200)

        assert len(event_id) <= 100
        uuid.UUID(event_id)  # and is a well-formed UUID


class TestTheIdentityTheDesignRestsOn:
    """specs/010 research R5, O-A. If a langchain upgrade changes how a message id is assigned, these
    fail loudly, instead of the meter quietly double-counting or merging calls."""

    @staticmethod
    def _chat_openai(provider_id="chatcmpl-SAME-EVERY-TIME"):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": provider_id,
                    "object": "chat.completion",
                    "created": 0,
                    "model": "chat",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                },
            )

        return ChatOpenAI(
            model="chat",
            api_key="k",
            base_url="http://gw/v1",
            max_retries=0,
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_two_identical_calls_get_different_ids_even_when_the_provider_repeats_its_own(self):
        llm = self._chat_openai()

        first = await llm.ainvoke([HumanMessage(content="a")])
        second = await llm.ainvoke([HumanMessage(content="a")])

        assert first.response_metadata["id"] == second.response_metadata["id"], "the provider repeated itself"
        assert first.id != second.id
        assert first.id.startswith("run-") and second.id.startswith("run-")

    async def test_the_message_id_is_not_the_providers_id(self):
        response = await self._chat_openai().ainvoke([HumanMessage(content="a")])

        assert response.id != response.response_metadata["id"]

    async def test_the_id_is_identical_after_a_checkpoint_round_trip(self):
        llm = self._chat_openai()

        class State(TypedDict):
            messages: Annotated[list, add_messages]

        async def agent(state):
            return {"messages": [await llm.ainvoke(state["messages"])]}

        builder = StateGraph(State)
        builder.add_node("agent", agent)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", END)
        graph = builder.compile(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t"}}

        out = await graph.ainvoke({"messages": [HumanMessage(content="q")]}, cfg)
        live = next(m for m in out["messages"] if isinstance(m, AIMessage))
        reread = next(m for m in (await graph.aget_state(cfg)).values["messages"] if isinstance(m, AIMessage))

        assert live.id == reread.id

    async def test_the_event_id_can_be_derived_from_what_the_checkpoint_stores(self, usage_event_sink):
        """The property that makes a replay recognisable: the id recorded at call time is derivable
        from the message that lands in the checkpoint."""
        call = await metering.metered_invoke(
            self._chat_openai(), [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias="chat"
        )

        (row,) = usage_event_sink
        assert row["event_id"] == usage_events.event_id_for(TEST_CTX["tenant"], call.response.id)


class TestWhatAnEventRecords:
    async def test_one_call_is_one_row_with_its_tokens_cost_and_price(self, usage_event_sink, monkeypatch):
        _price_models(monkeypatch, chat=(0.001, 0.002))

        await metering.metered_invoke(
            _llm(_reply(input_tokens=100, output_tokens=50)), [HumanMessage(content="q")],
            config=CONFIG, kind="chat", model_alias="chat",
        )

        (row,) = usage_event_sink
        assert (row["tenant"], row["principal"], row["thread_id"]) == (
            TEST_CTX["tenant"], TEST_CTX["principal"], "thread-1",
        )
        assert (row["kind"], row["model_alias"]) == ("chat", "chat")
        assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (100, 50, 150)
        assert row["cost_usd"] == pytest.approx(0.2)  # 100 * 0.001 + 50 * 0.002
        assert (row["price_input_per_token"], row["price_output_per_token"]) == (0.001, 0.002)

    async def test_the_concrete_model_behind_the_alias_is_recorded(self, usage_event_sink, monkeypatch):
        """Pattern 38: an alias can be re-pointed at another model, so what a call actually ran on is written down with it.
        (This was the per-turn ledger's test until the ledger write was retired; the property moved with the money.)"""

        class Catalog:
            async def resolve(self, alias):
                assert alias == "gpt-4o"
                return "openai/gpt-4o-2024-08-06"

        with model_ports.model_catalog.overriding(Catalog()):  # type: ignore[arg-type] - only `resolve` is asked of it here
            await metering.metered_invoke(
                _llm(_reply()), [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias="gpt-4o"
            )

        (row,) = usage_event_sink
        assert (row["model_alias"], row["resolved_model"]) == ("gpt-4o", "openai/gpt-4o-2024-08-06")

    async def test_a_model_that_cannot_be_resolved_is_recorded_as_null_not_skipped(self, usage_event_sink):
        """The conftest default resolves nothing, which is also what LiteLLM being down looks like: the event is still written."""
        await metering.metered_invoke(
            _llm(_reply()), [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias="chat"
        )

        (row,) = usage_event_sink
        assert row["resolved_model"] is None and row["total_tokens"] > 0

    async def test_cached_input_is_carved_out_and_recorded(self, usage_event_sink, monkeypatch):
        _price_models(monkeypatch, chat=(0.001, 0.002))

        await metering.metered_invoke(
            _llm(_reply(input_tokens=100, output_tokens=0, cached=40)), [HumanMessage(content="q")],
            config=CONFIG, kind="chat", model_alias="chat",
        )

        (row,) = usage_event_sink
        assert row["cached_input_tokens"] == 40

    async def test_an_unpriced_model_is_recorded_as_null_never_zero_and_counted_once(
        self, usage_event_sink, monkeypatch
    ):
        """A 0 here would be billed as free. NULL says "we do not know"."""
        _price_models(monkeypatch)  # no model has a price
        before = metric_value(metrics.agent_unpriced_usage_total, model_alias="mystery")

        await metering.metered_invoke(
            _llm(_reply()), [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias="mystery"
        )

        (row,) = usage_event_sink
        assert row["cost_usd"] is None
        assert row["price_input_per_token"] is None and row["price_output_per_token"] is None
        assert metric_value(metrics.agent_unpriced_usage_total, model_alias="mystery") == before + 1

    async def test_the_caller_still_gets_zero_for_an_unpriced_call_in_its_running_total(self, monkeypatch):
        """The in-run ceiling and the ledger row keep their old convention (unknown adds 0.0)."""
        _price_models(monkeypatch)

        call = await metering.metered_invoke(
            _llm(_reply()), [HumanMessage(content="q")], config=CONFIG, kind="chat", model_alias="mystery"
        )

        assert call.cost_usd == 0.0
        assert call.priced.cost_usd is None

    async def test_the_node_records_the_kind_it_was_built_with(self, usage_event_sink):
        await make_agent_node(_llm(_reply()), kind="subagent")({"messages": [HumanMessage(content="hi")]}, CONFIG)

        assert [row["kind"] for row in usage_event_sink] == ["subagent"]

    async def test_a_real_delegated_run_is_recorded_as_a_subagent_call(self, usage_event_sink):
        """Through `run_subagent`'s own wiring (`GraphDeps.meter_kind` -> `make_agent_node`), not a
        node built by hand: a delegated run's spend must be separable from the parent's."""
        from pathlib import Path

        from app.agent.tools.subagent_catalog import SubagentRecord
        from tests.ecorp import run_subagent_impl

        record = SubagentRecord(
            name="researcher", description="d", system_prompt="You are a test subagent.",
            tools=("calculator",), model=None, domains=None, path=Path("subagents/researcher/AGENT.md"),
        )

        result = await run_subagent_impl(
            "researcher", "a task", CONFIG, registry={"researcher": (record, ("calculator",))},
            llm=_llm(_reply("The delegated researcher found that the answer is forty-two."))
        )

        assert "forty-two" in result.answer
        assert [row["kind"] for row in usage_event_sink] == ["subagent"]
        assert ":subagent:researcher:" in usage_event_sink[0]["thread_id"]


class TestWhenNothingCanBeMetered:
    async def test_without_a_ctx_nothing_is_written(self, usage_event_sink):
        await metering.metered_invoke(
            _llm(_reply()), [HumanMessage(content="q")], config=None, kind="chat", model_alias="chat"
        )

        assert usage_event_sink == []

    async def test_a_call_that_reported_no_usage_is_counted_not_written(self, usage_event_sink):
        before = _count("usage_missing")

        await metering.metered_invoke(
            _llm(AIMessage(content="no usage reported")), [HumanMessage(content="q")],
            config=CONFIG, kind="chat", model_alias="chat",
        )

        assert usage_event_sink == []
        assert _count("usage_missing") == before + 1

    async def test_a_response_with_no_message_id_is_still_recorded_but_counted(self, usage_event_sink):
        """It cannot be de-duplicated, so a replay would double-count it: visible, not silent."""
        before = _count("usage_event_identity")
        priced = await pricing.price_call("chat", _usage())

        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id=None, kind="chat", model_alias="chat", priced=priced
        )

        assert len(usage_event_sink) == 1
        assert _count("usage_event_identity") == before + 1

    async def test_an_unknown_kind_is_refused_and_counted(self, usage_event_sink):
        before = _count("usage_event_write")
        priced = await pricing.price_call("chat", _usage())

        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id="m", kind="chta", model_alias="chat", priced=priced
        )

        assert usage_event_sink == []
        assert _count("usage_event_write") == before + 1


class _FailingStore(InMemoryUsageEventStore):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def insert(self, row):
        raise self.error


class TestAFailedWriteNeverFailsTheTurnAndIsNeverSilent:
    @staticmethod
    async def _record():
        priced = await pricing.price_call("chat", _usage())
        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id="m", kind="chat", model_alias="chat", priced=priced
        )

    async def test_a_database_error_is_swallowed_and_counted_as_a_write_failure(self):
        before = _count("usage_event_write")

        with cost_ports.usage_events.overriding(_FailingStore(ConnectionError("appdata unreachable"))):
            await self._record()  # does not raise

        assert _count("usage_event_write") == before + 1

    async def test_a_missing_table_is_its_own_counted_path_and_warns_once(self, caplog):
        before = _count("usage_event_table_missing")
        write_before = _count("usage_event_write")

        with cost_ports.usage_events.overriding(_FailingStore(UsageTableMissing())):
            with caplog.at_level("WARNING", logger=usage_events.logger.name):
                for _ in range(3):
                    await self._record()

        assert _count("usage_event_table_missing") == before + 3
        assert _count("usage_event_write") == write_before, "an unapplied migration is not a write failure"
        assert len([r for r in caplog.records if "table is missing" in r.getMessage()]) == 1

    async def test_the_agent_node_still_answers_when_the_write_fails(self):
        with cost_ports.usage_events.overriding(_FailingStore(ConnectionError("appdata unreachable"))):
            result = await make_agent_node(_llm(_reply("the answer")))({"messages": [HumanMessage(content="q")]}, CONFIG)

        assert result["messages"][0].content == "the answer"


class TestTheEventsAddUpToWhatTheLedgerRowIsWrittenFrom:
    """The dual write (module docstring): the per-turn ledger row is written from the agent node's
    running total, so if the per-call events add up to that total they cannot disagree."""

    async def test_a_two_call_turn_is_two_events_whose_sums_equal_the_state_totals(
        self, usage_event_sink, monkeypatch
    ):
        from app.core.config import CHAT_MODEL

        _price_models(monkeypatch, **{CHAT_MODEL: (0.001, 0.002)})
        tool_call = AIMessage(
            content="",
            tool_calls=[{"name": "calculator", "args": {"expression": "6*7"}, "id": "c1"}],
            usage_metadata=_usage(input_tokens=200, output_tokens=20),
        )
        final = _reply("The answer is 42, as computed above.", input_tokens=300, output_tokens=30)
        graph = build_graph(GraphDeps(llm=_llm(tool_call, final)))

        state = await graph.ainvoke({"messages": [HumanMessage(content="what is 6*7?")]}, config=_config())

        assert len(usage_event_sink) == state["iterations"] == 2
        assert len({row["event_id"] for row in usage_event_sink}) == 2
        assert sum(row["cost_usd"] for row in usage_event_sink) == pytest.approx(state["total_cost_usd"])
        assert sum(row["total_tokens"] for row in usage_event_sink) == state["total_tokens"]
        assert len({row["thread_id"] for row in usage_event_sink}) == 1, "one turn, one thread"
