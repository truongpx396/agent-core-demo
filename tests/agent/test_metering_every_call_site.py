"""Follow-up suggestions, history compaction and the cron scripts are metered (specs/010 research G1).

Before: these called the model and threw the usage away, so the spend reached no ledger, no dollar cap
and no billing meter. Now each is one `metering.metered_invoke` call that records a usage event and,
because the cost never joins the agent node's per-turn running total, its own ledger row.

The invariant that matters, and the one a double-count or a gap would break: **every call's cost is
counted exactly once**, either inside the turn's running total (the agent node, whose ledger row is
written at the end of the turn) or in its own ledger row (everything else).
"""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent import metering, pricing
from app.agent.graph import GraphDeps
from app.agent.graph_agent_node import make_agent_node
from app.agent.graph_build import build_graph
from app.agent.graph_compaction import _estimate_tokens, make_compact_history_node
from app.agent.graph_followups import make_suggest_followups_node
from tests.agent.test_graph_integration import TestHistorySummarization, _config
from tests.conftest import TEST_CTX

CONFIG = {"configurable": {"ctx": TEST_CTX, "thread_id": "thread-1"}}


@pytest.fixture
def ledger_rows(monkeypatch):
    """The ledger rows the choke point writes for calls that carry their own (`to_ledger=True`)."""
    rows: list[tuple] = []

    async def record_usage(ctx, thread_id, model_alias, total_tokens, cost_usd):
        rows.append((ctx, thread_id, model_alias, total_tokens, cost_usd))

    monkeypatch.setattr(metering, "record_usage", record_usage)
    return rows


@pytest.fixture(autouse=True)
def priced_chat(monkeypatch):
    from app.core.config import CHAT_MODEL

    async def fetch():
        return [
            {"model_name": CHAT_MODEL, "model_info": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}}
        ]

    monkeypatch.setattr(pricing, "_fetch_model_info", fetch)
    pricing.reset_pricing_state()


def _reply(text, input_tokens, output_tokens):
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def _llm(*messages):
    return GenericFakeChatModel(messages=iter(messages))


def _grounded_state():
    return {"messages": [AIMessage(content="A grounded answer about checkpointers.")], "used_citations": ["doc-1"]}


def _history(turns=3):
    messages = [SystemMessage(content="seed", id="sys")]
    for i in range(turns):
        messages.append(HumanMessage(content=f"question number {i} with some real words", id=f"h{i}"))
        messages.append(AIMessage(content=f"answer number {i} with some real words too", id=f"a{i}"))
    return messages


def _tripped_ceiling(messages):
    return _estimate_tokens([m for m in messages if not isinstance(m, SystemMessage)]) - 1


class TestFollowUps:
    async def test_one_call_is_one_event_and_one_ledger_row_with_the_same_cost(self, usage_event_sink, ledger_rows):
        llm = _llm(_reply("What next?\nAnd then?", input_tokens=200, output_tokens=20))

        result = await make_suggest_followups_node(llm)(_grounded_state(), CONFIG)

        assert result == {"followups": ["What next?", "And then?"]}
        (event,) = usage_event_sink
        assert event["kind"] == "followups"
        (row,) = ledger_rows
        assert row[:3] == (TEST_CTX, "thread-1", event["model_alias"])
        assert row[3] == event["total_tokens"] == 220
        assert row[4] == pytest.approx(event["cost_usd"]) == pytest.approx(0.24)  # 200*0.001 + 20*0.002

    async def test_it_is_recorded_under_the_alias_it_was_built_for(self, usage_event_sink, ledger_rows):
        await make_suggest_followups_node(_llm(_reply("Q?", 100, 10)), model_alias="special-alias")(
            _grounded_state(), CONFIG
        )

        assert [e["model_alias"] for e in usage_event_sink] == ["special-alias"]
        assert [row[2] for row in ledger_rows] == ["special-alias"]

    async def test_it_does_not_touch_the_turns_running_totals(self, ledger_rows):
        result = await make_suggest_followups_node(_llm(_reply("Q?", 200, 20)))(_grounded_state(), CONFIG)

        assert "total_tokens" not in result and "total_cost_usd" not in result

    async def test_a_cache_hit_makes_no_call_and_so_records_nothing(self, usage_event_sink, ledger_rows):
        await make_suggest_followups_node(_llm())({**_grounded_state(), "cache_hit": True}, CONFIG)

        assert usage_event_sink == [] and ledger_rows == []


class TestCompaction:
    async def test_one_call_is_one_event_and_one_ledger_row(self, usage_event_sink, ledger_rows):
        messages = _history()
        node = make_compact_history_node(
            _llm(_reply("a running summary", input_tokens=1500, output_tokens=40)),
            ceiling=_tripped_ceiling(messages),
            floor=1,
        )

        result = await node({"messages": messages}, CONFIG)

        assert result["history_summary"] == "a running summary"
        (event,) = usage_event_sink
        assert event["kind"] == "compaction"
        (row,) = ledger_rows
        assert row[3] == event["total_tokens"] == 1540
        assert row[4] == pytest.approx(event["cost_usd"])

    async def test_a_long_summary_never_eats_the_turns_own_safety_budget(self, ledger_rows):
        """Compaction can summarise thousands of tokens. Adding that to the turn's token or cost budget
        could stop a perfectly good answer, so it is recorded separately and never added to them."""
        messages = _history()
        node = make_compact_history_node(
            _llm(_reply("a summary", input_tokens=50_000, output_tokens=500)), ceiling=_tripped_ceiling(messages), floor=1
        )

        result = await node({"messages": messages}, CONFIG)

        assert "total_tokens" not in result and "total_cost_usd" not in result
        assert len(ledger_rows) == 1

    async def test_a_turn_that_does_not_compact_makes_no_call(self, usage_event_sink, ledger_rows):
        messages = _history(1)
        node = make_compact_history_node(_llm(), ceiling=_estimate_tokens(messages) + 100, floor=1)

        assert await node({"messages": messages}, CONFIG) == {}
        assert usage_event_sink == [] and ledger_rows == []


class TestEveryCallIsCountedExactlyOnce:
    async def test_the_agent_node_writes_no_ledger_row_of_its_own(self, usage_event_sink, ledger_rows):
        """Its cost joins the turn's running total, which the turn-end ledger row is written from;
        a row here too would count it twice."""
        await make_agent_node(_llm(_reply("the answer", 100, 50)))({"messages": [HumanMessage(content="q")]}, CONFIG)

        assert len(usage_event_sink) == 1
        assert ledger_rows == []

    async def test_a_turn_with_an_answer_a_summary_and_follow_ups_adds_up_with_no_gap_and_no_overlap(
        self, usage_event_sink, ledger_rows
    ):
        messages = _history()
        compaction = make_compact_history_node(
            _llm(_reply("a summary", 1500, 40)), ceiling=_tripped_ceiling(messages), floor=1
        )
        await compaction({"messages": messages}, CONFIG)
        state = await make_agent_node(_llm(_reply("The final answer.", 300, 60)))(
            {"messages": [HumanMessage(content="q")]}, CONFIG
        )
        await make_suggest_followups_node(_llm(_reply("Q1?\nQ2?", 120, 15)))(_grounded_state(), CONFIG)

        in_turn_total = state["total_cost_usd"]  # what the turn-end ledger row is written from
        own_rows = sum(row[4] for row in ledger_rows)  # compaction + follow-ups
        assert len(usage_event_sink) == 3
        assert sum(e["cost_usd"] for e in usage_event_sink) == pytest.approx(in_turn_total + own_rows)


class TestACallWithNoTenantIsNeverRecorded:
    async def test_without_a_ctx_neither_the_event_nor_the_ledger_row_is_written(self, usage_event_sink, ledger_rows):
        await make_suggest_followups_node(_llm(_reply("Q?", 100, 10)))(_grounded_state(), None)

        assert usage_event_sink == []
        assert [row for row in ledger_rows if row[0] is not None] == []


class TestTheGraphThreadsTheClientsOwnAliasToEverySpender:
    async def test_compaction_and_the_answer_are_recorded_under_the_alias_the_client_talks_to(
        self, usage_event_sink, ledger_rows
    ):
        """`GraphDeps.model_alias` names the model this client really calls (a delegated run whose
        specialist declares its own). A node that fell back to the global chat alias would price and
        record that spend under the wrong model."""
        cls = TestHistorySummarization
        answers = [_reply(f"Answer number {i}, long enough to pass the length check.", 100, 20) for i in range(1, cls.N_TURNS + 1)]
        ceiling, floor, questions = cls._budget_for(answers)
        llm = _llm(*answers, _reply("The user first asked question 1.", 400, 30), _reply("Answer number 4, long enough to pass.", 100, 20))
        graph = build_graph(
            GraphDeps(llm=llm, model_alias="special-alias"), history_token_ceiling=ceiling, history_token_floor=floor
        )
        config = _config()

        for question in questions:
            await graph.ainvoke({"messages": [HumanMessage(content=question)]}, config=config)

        by_kind = {event["kind"] for event in usage_event_sink}
        assert {"chat", "compaction"} <= by_kind, "the triggering turn must have compacted"
        assert {event["model_alias"] for event in usage_event_sink} == {"special-alias"}
        assert {row[2] for row in ledger_rows} == {"special-alias"}


def test_build_graph_hands_the_clients_alias_to_the_follow_up_node(monkeypatch):
    """Follow-ups only run for a grounded answer, which is awkward to provoke through a whole graph;
    what can go wrong at the seam is the argument not being passed, so capture it."""
    from app.agent import graph_build

    seen = {}
    real = graph_build.make_suggest_followups_node

    def capture(llm, model_alias=None):
        seen["alias"] = model_alias
        return real(llm, model_alias=model_alias)

    monkeypatch.setattr(graph_build, "make_suggest_followups_node", capture)

    build_graph(GraphDeps(llm=_llm(), model_alias="special-alias"))

    assert seen == {"alias": "special-alias"}
