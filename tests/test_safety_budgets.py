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
from langchain_core.messages import AIMessage, HumanMessage

from app import tools
from app.graph import (
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    should_continue,
    too_many_tool_calls,
    validate_input,
)


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


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
        assert validate_input(state) == {"iterations": 0, "total_tokens": 0}


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
        for msg, call in zip(result["messages"], calls):
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
