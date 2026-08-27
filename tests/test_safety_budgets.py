"""Tests for the multi-layer safety budgets: per-turn iteration/token
counters actually reset between turns, the tool-call-per-turn cap, the
token-per-turn cap, and the tool execution timeout.

Request-level wall-clock timeout (REQUEST_TIMEOUT_SECONDS, app/agent.py)
isn't exercised here since it needs the real ChatOpenAI client / live
services to observe meaningfully — `_iterate_with_timeout` itself is a
thin wrapper over `asyncio.wait_for`, which is well-covered by asyncio's
own test suite.
"""
import time

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from app import metrics, tools
from app.graph import (
    MAX_HISTORY_TURNS,
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    _trim_history,
    make_compact_history_node,
    should_continue,
    too_many_tool_calls,
    validate_input,
)
from tests.conftest import TEST_CTX


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def _cfg(ctx=TEST_CTX):
    return {"configurable": {"ctx": ctx}}


class TestPerTurnReset:
    def test_validate_input_resets_iterations_and_tokens(self):
        """Regression test: without this reset, iterations/total_tokens
        persist in the checkpointed thread state and climb turn over turn,
        eventually tripping MAX_ITERATIONS/MAX_TOKENS_PER_TURN on an
        unrelated future turn regardless of that turn's actual work."""
        state = {
            "messages": [HumanMessage(content="hi")],
            "iterations": 7,
            "total_tokens": 5000,
        }
        result = validate_input(state, _cfg())
        assert result["iterations"] == 0
        assert result["total_tokens"] == 0

    def test_validate_input_generates_a_fresh_run_id_every_turn(self):
        """run_id correlates this turn's node lifecycle logs (_instrumented)
        — it must change turn over turn, same reset point as
        iterations/total_tokens, or logs from different turns on a
        long-running thread would be indistinguishable."""
        state = {"messages": [HumanMessage(content="hi")], "run_id": "stale"}
        result = validate_input(state, _cfg())
        assert result["run_id"] and result["run_id"] != "stale"

    def test_validate_input_stamps_ctx_from_config(self):
        """ctx comes from config["configurable"]["ctx"] — the trusted
        boundary (app/api.py's header extraction, or a local dev ctx) —
        never derived from state/message content. See State's docstring
        for why this is the only node allowed to write it."""
        state = {"messages": [HumanMessage(content="hi")]}
        result = validate_input(state, _cfg())
        assert result["ctx"] == TEST_CTX

    def test_validate_input_stamps_none_when_config_has_no_ctx(self):
        state = {"messages": [HumanMessage(content="hi")]}
        result = validate_input(state, {"configurable": {}})
        assert result["ctx"] is None


class TestHistoryBudget:
    """The only unbounded input in State (`messages`) must not grow forever
    across a long-running thread — see MAX_HISTORY_TURNS / _trim_history in
    app/graph.py."""

    @staticmethod
    def _turns(n):
        messages = [SystemMessage(content="seed", id="sys")]
        for i in range(n):
            messages.append(HumanMessage(content=f"question {i}", id=f"h{i}"))
            messages.append(AIMessage(content=f"answer {i}", id=f"a{i}"))
        return messages

    def test_within_budget_trims_nothing(self):
        assert _trim_history(self._turns(MAX_HISTORY_TURNS)) == []

    def test_over_budget_drops_oldest_whole_turns_only(self):
        messages = self._turns(MAX_HISTORY_TURNS + 2)
        removed = _trim_history(messages)
        removed_ids = {rm.id for rm in removed}
        assert all(isinstance(rm, RemoveMessage) for rm in removed)

        # The two oldest turns (h0/a0, h1/a1) are dropped whole ...
        assert removed_ids == {"h0", "a0", "h1", "a1"}
        # ... the seeded system message is never touched ...
        assert "sys" not in removed_ids
        # ... and every remaining turn survives untouched.
        kept_ids = {f"h{i}" for i in range(2, MAX_HISTORY_TURNS + 2)} | {
            f"a{i}" for i in range(2, MAX_HISTORY_TURNS + 2)
        }
        assert removed_ids.isdisjoint(kept_ids)

    def test_message_without_id_is_left_alone(self):
        """RemoveMessage deletes by id — a message with none (only possible
        outside a compiled graph) can't be targeted, so it must not crash
        or be silently mismatched to the wrong message."""
        messages = self._turns(MAX_HISTORY_TURNS + 1)
        messages[1].id = None  # the oldest turn's HumanMessage
        removed = _trim_history(messages)
        assert None not in {rm.id for rm in removed}

    def test_validate_input_no_longer_touches_messages(self):
        """Trimming/summarization moved to compact_history (see below) —
        validate_input stays a plain, dependency-free function of
        state/config with no LLM call of its own."""
        state = {"messages": self._turns(MAX_HISTORY_TURNS + 1)}
        result = validate_input(state, _cfg())
        assert "messages" not in result


class TestCompactHistoryNode:
    """compact_history (app/graph.py) replaced validate_input's old
    discard-only trim with a discard-AND-summarize node — see
    _messages_to_trim (the shared "what falls outside the window" helper)
    and make_compact_history_node's docstring."""

    @staticmethod
    def _turns(n):
        messages = [SystemMessage(content="seed", id="sys")]
        for i in range(n):
            messages.append(HumanMessage(content=f"question {i}", id=f"h{i}"))
            messages.append(AIMessage(content=f"answer {i}", id=f"a{i}"))
        return messages

    def test_applies_the_trim_and_increments_metric(self):
        before = metrics.agent_history_compacted_total._value.get()
        compact_history = make_compact_history_node(
            GenericFakeChatModel(messages=iter([AIMessage(content="a summary")]))
        )
        state = {"messages": self._turns(MAX_HISTORY_TURNS + 1)}
        result = compact_history(state)

        assert "messages" in result
        assert all(isinstance(m, RemoveMessage) for m in result["messages"])
        assert result["history_summary"] == "a summary"
        assert metrics.agent_history_compacted_total._value.get() == before + 1

    def test_returns_nothing_when_within_budget(self):
        compact_history = make_compact_history_node(GenericFakeChatModel(messages=iter([])))
        state = {"messages": self._turns(1)}
        assert compact_history(state) == {}

    def test_degrades_to_trimming_without_a_summary_on_llm_failure(self):
        class _BoomLLM:
            def invoke(self, messages):
                raise RuntimeError("boom")

        compact_history = make_compact_history_node(_BoomLLM())
        state = {"messages": self._turns(MAX_HISTORY_TURNS + 1)}
        result = compact_history(state)

        assert "messages" in result
        assert all(isinstance(m, RemoveMessage) for m in result["messages"])
        assert "history_summary" not in result

    def test_extends_a_prior_summary_rather_than_replacing_it(self):
        captured = {}

        class _RecordingLLM:
            def invoke(self, messages):
                captured["prompt"] = messages[0].content
                return AIMessage(content="an extended summary")

        compact_history = make_compact_history_node(_RecordingLLM())
        state = {
            "messages": self._turns(MAX_HISTORY_TURNS + 1),
            "history_summary": "earlier summary text",
        }
        result = compact_history(state)

        assert "earlier summary text" in captured["prompt"]
        assert result["history_summary"] == "an extended summary"


class TestToolCallBudget:
    def test_within_budget_routes_normally(self):
        state = {
            "iterations": 1,
            "messages": [_tool_call_message("calculator", {"expression": "1+1"})],
        }
        assert should_continue(state) == "tools"

    def test_exceeding_budget_routes_to_too_many_tool_calls(self):
        calls = [
            {"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}
            for i in range(MAX_TOOL_CALLS_PER_TURN + 1)
        ]
        state = {"iterations": 1, "messages": [AIMessage(content="", tool_calls=calls)]}
        assert should_continue(state) == "too_many_tool_calls"

    def test_at_exactly_the_limit_still_routes_normally(self):
        calls = [
            {"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}
            for i in range(MAX_TOOL_CALLS_PER_TURN)
        ]
        state = {"iterations": 1, "messages": [AIMessage(content="", tool_calls=calls)]}
        assert should_continue(state) == "tools"

    def test_too_many_tool_calls_node_rejects_every_pending_call(self):
        calls = [
            {"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}
            for i in range(MAX_TOOL_CALLS_PER_TURN + 2)
        ]
        ai = AIMessage(content="", tool_calls=calls)
        result = too_many_tool_calls({"messages": [ai]})
        assert len(result["messages"]) == len(calls)
        for msg, call in zip(result["messages"], calls, strict=True):
            assert msg.tool_call_id == call["id"]
            assert "too many tool calls" in msg.content.lower()


class TestTokenBudget:
    def test_under_budget_continues_normally(self):
        state = {
            "iterations": 1,
            "total_tokens": MAX_TOKENS_PER_TURN - 1,
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "check_output"

    def test_over_budget_ends_even_with_a_pending_tool_call(self):
        state = {
            "iterations": 1,
            "total_tokens": MAX_TOKENS_PER_TURN,
            "messages": [_tool_call_message("calculator", {"expression": "1+1"})],
        }
        assert should_continue(state) == "__end__"

    def test_missing_total_tokens_defaults_to_zero(self):
        state = {"iterations": 1, "messages": [AIMessage(content="final answer here.")]}
        assert should_continue(state) == "check_output"


class TestToolTimeout:
    def test_fast_call_returns_normally(self, monkeypatch):
        monkeypatch.setattr(tools, "TOOL_TIMEOUT_SECONDS", 5)
        assert tools._run_with_timeout(lambda: 42) == 42

    def test_slow_call_raises_timeout_error(self, monkeypatch):
        monkeypatch.setattr(tools, "TOOL_TIMEOUT_SECONDS", 0.05)

        def slow():
            time.sleep(0.5)
            return "too slow"

        with pytest.raises(TimeoutError):
            tools._run_with_timeout(slow)
