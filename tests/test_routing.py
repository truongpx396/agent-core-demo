"""Unit tests for the graph's conditional-edge functions.

Each of these is a plain function of a hand-built state dict — no LLM call,
no graph compile, no I/O — so the routing logic is tested the boring,
deterministic way it deserves.
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import (
    MAX_ITERATIONS,
    _mandatory_gate_reason,
    _tool_capability,
    route_after_approval,
    route_after_cache,
    route_after_check,
    route_after_moderation,
    route_after_validation,
    should_continue,
)
from tests.conftest import TEST_CTX


def _tool_call(name, call_id="1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": call_id}],
    )


class TestRouteAfterValidation:
    def test_empty_input_rejected(self):
        state = {"messages": [HumanMessage(content="")], "ctx": TEST_CTX}
        assert route_after_validation(state) == "reject_input"

    def test_whitespace_only_input_rejected(self):
        state = {"messages": [HumanMessage(content="   ")], "ctx": TEST_CTX}
        assert route_after_validation(state) == "reject_input"

    def test_no_human_message_rejected(self):
        state = {"messages": [AIMessage(content="hello")], "ctx": TEST_CTX}
        assert route_after_validation(state) == "reject_input"

    def test_valid_input_goes_to_moderation_first(self):
        state = {
            "messages": [HumanMessage(content="What is our refund policy?")],
            "ctx": TEST_CTX,
        }
        assert route_after_validation(state) == "moderate_input"

    def test_uses_last_human_message(self):
        state = {
            "ctx": TEST_CTX,
            "messages": [
                HumanMessage(content=""),
                AIMessage(content="anything"),
                HumanMessage(content="a real question"),
            ]
        }
        assert route_after_validation(state) == "moderate_input"


class TestRouteAfterModeration:
    def test_blocked_routes_to_reject_moderation(self):
        assert route_after_moderation({"moderation_blocked": True}) == "reject_moderation"

    def test_allowed_routes_to_check_semantic_cache(self):
        assert route_after_moderation({"moderation_blocked": False}) == "check_semantic_cache"

    def test_absent_flag_defaults_to_allowed(self):
        assert route_after_moderation({}) == "check_semantic_cache"


class TestRouteAfterCache:
    def test_hit_skips_straight_to_check_output(self):
        assert route_after_cache({"cache_hit": True}) == "check_output"

    def test_miss_proceeds_to_retrieve_context(self):
        assert route_after_cache({"cache_hit": False}) == "retrieve_context"

    def test_absent_flag_defaults_to_a_miss(self):
        assert route_after_cache({}) == "retrieve_context"


class TestRouteAfterValidationCtx:
    """SecurityCtx is checked before the message itself — see
    route_after_validation's docstring in app/graph.py for why a missing
    ctx gets its own outcome (reject_context) rather than being folded
    into reject_input's "you typed nothing.\""""

    def test_missing_ctx_rejected(self):
        state = {"messages": [HumanMessage(content="a real question")]}
        assert route_after_validation(state) == "reject_context"

    def test_none_ctx_rejected(self):
        state = {"messages": [HumanMessage(content="a real question")], "ctx": None}
        assert route_after_validation(state) == "reject_context"

    def test_ctx_missing_tenant_rejected(self):
        state = {
            "messages": [HumanMessage(content="a real question")],
            "ctx": {"tenant": "", "principal": "u1", "claims": {}},
        }
        assert route_after_validation(state) == "reject_context"

    def test_ctx_missing_principal_rejected(self):
        state = {
            "messages": [HumanMessage(content="a real question")],
            "ctx": {"tenant": "acme", "principal": "", "claims": {}},
        }
        assert route_after_validation(state) == "reject_context"

    def test_missing_ctx_checked_before_empty_message(self):
        """Both are invalid here — a missing ctx must win, since it's the
        more fundamental "who is asking" gate (see the docstring)."""
        state = {"messages": [HumanMessage(content="")]}
        assert route_after_validation(state) == "reject_context"


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


class TestToolCapability:
    def test_known_read_only_tools(self):
        assert _tool_capability("search_docs") == "read_only"
        assert _tool_capability("calculator") == "read_only"

    def test_known_mutating_tool(self):
        assert _tool_capability("add_note") == "mutating"

    def test_unknown_tool_defaults_to_outward(self):
        """Fail closed: a tool added to TOOLS without a TOOL_CAPABILITIES
        entry must be gated, not silently trusted."""
        assert _tool_capability("some_new_tool_nobody_registered") == "outward"


class TestMandatoryGateReason:
    def test_all_read_only_is_none(self):
        calls = [{"name": "search_docs"}, {"name": "calculator"}]
        assert _mandatory_gate_reason(calls) is None

    def test_one_mutating_call_is_mutating(self):
        calls = [{"name": "search_docs"}, {"name": "add_note"}]
        assert _mandatory_gate_reason(calls) == "mutating"

    def test_undeclared_tool_outranks_mutating(self):
        calls = [{"name": "add_note"}, {"name": "totally_unknown"}]
        assert _mandatory_gate_reason(calls) == "outward"


class TestShouldContinueMandatoryGate:
    """A mutating/outward tool call must route to human_approval even when
    require_approval is False (the default) — mandatory, not opt-in. See
    app/tools.py::TOOL_CAPABILITIES and should_continue's docstring."""

    def test_mutating_tool_gates_even_without_require_approval(self):
        state = {
            "iterations": 1,
            "require_approval": False,
            "messages": [_tool_call("add_note")],
        }
        assert should_continue(state) == "human_approval"

    def test_undeclared_tool_gates_even_without_require_approval(self):
        state = {
            "iterations": 1,
            "require_approval": False,
            "messages": [_tool_call("some_new_tool_nobody_registered")],
        }
        assert should_continue(state) == "human_approval"

    def test_read_only_tool_still_runs_directly_without_require_approval(self):
        """Regression guard: the mandatory gate must not become an
        accidental gate-everything — read-only tools keep the pre-existing
        opt-in-only behavior."""
        state = {
            "iterations": 1,
            "require_approval": False,
            "messages": [_tool_call("calculator")],
        }
        assert should_continue(state) == "tools"

    def test_mixed_batch_with_one_mutating_call_gates_the_whole_batch(self):
        state = {
            "iterations": 1,
            "require_approval": False,
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "calculator", "args": {}, "id": "1"},
                        {"name": "add_note", "args": {}, "id": "2"},
                    ],
                )
            ],
        }
        assert should_continue(state) == "human_approval"


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

    def test_long_answer_goes_to_suggest_followups(self):
        state = {"messages": [AIMessage(content="A sufficiently detailed answer.")]}
        assert route_after_check(state) == "suggest_followups"
