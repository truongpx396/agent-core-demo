"""Unit tests for the graph's conditional-edge functions.

Each of these is a plain function of a hand-built state dict — no LLM call,
no graph compile, no I/O — so the routing logic is tested the boring,
deterministic way it deserves.
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import (
    MAX_ITERATIONS,
    route_after_approval,
    route_after_check,
    route_after_validation,
    should_continue,
)


class TestRouteAfterValidation:
    def test_empty_input_rejected(self):
        state = {"messages": [HumanMessage(content="")]}
        assert route_after_validation(state) == "reject_input"

    def test_whitespace_only_input_rejected(self):
        state = {"messages": [HumanMessage(content="   ")]}
        assert route_after_validation(state) == "reject_input"

    def test_no_human_message_rejected(self):
        state = {"messages": [AIMessage(content="hello")]}
        assert route_after_validation(state) == "reject_input"

    def test_valid_input_retrieves_context(self):
        state = {"messages": [HumanMessage(content="What is our refund policy?")]}
        assert route_after_validation(state) == "retrieve_context"

    def test_uses_last_human_message(self):
        state = {
            "messages": [
                HumanMessage(content=""),
                AIMessage(content="anything"),
                HumanMessage(content="a real question"),
            ]
        }
        assert route_after_validation(state) == "retrieve_context"


class TestShouldContinue:
    def test_iteration_limit_ends(self):
        state = {
            "iterations": MAX_ITERATIONS,
            "messages": [AIMessage(content="still going")],
        }
        assert should_continue(state) == "__end__"

    def test_over_iteration_limit_ends(self):
        state = {
            "iterations": MAX_ITERATIONS + 5,
            "messages": [AIMessage(content="still going")],
        }
        assert should_continue(state) == "__end__"

    def test_tool_call_routes_to_tools(self):
        state = {
            "iterations": 1,
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_docs", "args": {"query": "x"}, "id": "1"}
                    ],
                )
            ],
        }
        assert should_continue(state) == "tools"

    def test_tool_call_with_require_approval_routes_to_human_approval(self):
        state = {
            "iterations": 1,
            "require_approval": True,
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_docs", "args": {"query": "x"}, "id": "1"}
                    ],
                )
            ],
        }
        assert should_continue(state) == "human_approval"

    def test_final_answer_routes_to_check_output(self):
        state = {"iterations": 1, "messages": [AIMessage(content="The answer is 42.")]}
        assert should_continue(state) == "check_output"

    def test_missing_iterations_defaults_to_zero(self):
        state = {"messages": [AIMessage(content="The answer is 42.")]}
        assert should_continue(state) == "check_output"


class TestRouteAfterApproval:
    def test_approved_routes_to_tools(self):
        assert route_after_approval({"approved": True}) == "tools"

    def test_rejected_routes_to_agent(self):
        assert route_after_approval({"approved": False}) == "agent"

    def test_missing_approved_defaults_to_agent(self):
        assert route_after_approval({}) == "agent"


class TestRouteAfterCheck:
    def test_short_answer_retries(self):
        state = {"messages": [AIMessage(content="Yes.")]}
        assert route_after_check(state) == "retry_output"

    def test_empty_answer_retries(self):
        state = {"messages": [AIMessage(content="")]}
        assert route_after_check(state) == "retry_output"

    def test_long_answer_ends(self):
        state = {"messages": [AIMessage(content="A sufficiently detailed answer.")]}
        assert route_after_check(state) == "__end__"
