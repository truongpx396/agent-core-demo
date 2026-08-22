"""Unit tests for the graph's conditional-edge functions.

Each of these is a plain function of a hand-built state dict — no LLM call,
no graph compile, no I/O — so the routing logic is tested the boring,
deterministic way it deserves.
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import (
    MAX_HISTORY_SUMMARY_CHARS,
    MAX_ITERATIONS,
    MAX_REPEATED_ACTIONS,
    _consecutive_repeat_count,
    _mandatory_gate_reason,
    _tool_call_fingerprint,
    _tool_capability,
    route_after_approval,
    route_after_cache,
    route_after_check,
    route_after_compaction,
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

    def test_valid_input_goes_to_compact_history_first(self):
        state = {
            "messages": [HumanMessage(content="What is our refund policy?")],
            "ctx": TEST_CTX,
        }
        assert route_after_validation(state) == "compact_history"

    def test_uses_last_human_message(self):
        state = {
            "ctx": TEST_CTX,
            "messages": [
                HumanMessage(content=""),
                AIMessage(content="anything"),
                HumanMessage(content="a real question"),
            ]
        }
        assert route_after_validation(state) == "compact_history"


def _tc_batch(*name_args_pairs, call_id_prefix="c"):
    return [
        {"name": name, "args": args, "id": f"{call_id_prefix}{i}"}
        for i, (name, args) in enumerate(name_args_pairs)
    ]


class TestToolCallFingerprint:
    def test_identical_batches_fingerprint_identically(self):
        a = _tc_batch(("calculator", {"expression": "1+1"}))
        b = _tc_batch(("calculator", {"expression": "1+1"}), call_id_prefix="different")
        assert _tool_call_fingerprint(a) == _tool_call_fingerprint(b)

    def test_different_args_fingerprint_differently(self):
        a = _tc_batch(("calculator", {"expression": "1+1"}))
        b = _tc_batch(("calculator", {"expression": "2+2"}))
        assert _tool_call_fingerprint(a) != _tool_call_fingerprint(b)

    def test_call_order_within_a_batch_does_not_matter(self):
        a = _tc_batch(("calculator", {"expression": "1+1"}), ("search_docs", {"query": "x"}))
        b = _tc_batch(("search_docs", {"query": "x"}), ("calculator", {"expression": "1+1"}))
        assert _tool_call_fingerprint(a) == _tool_call_fingerprint(b)


class TestConsecutiveRepeatCount:
    def test_no_tool_calls_at_all_is_zero(self):
        messages = [HumanMessage(content="hi"), AIMessage(content="hello")]
        assert _consecutive_repeat_count(messages) == 0

    def test_a_single_tool_call_counts_as_one(self):
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"}))),
        ]
        assert _consecutive_repeat_count(messages) == 1

    def test_repeated_identical_calls_count_up(self):
        call = AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"})))
        messages = [HumanMessage(content="hi"), call, call, call]
        assert _consecutive_repeat_count(messages) == 3

    def test_a_different_call_resets_the_count(self):
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"}))),
            AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"}))),
            AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "2+2"}))),
        ]
        # Only the most recent (different) call counts — the repeat streak broke.
        assert _consecutive_repeat_count(messages) == 1

    def test_never_scans_across_a_prior_turns_human_message(self):
        """A model calling the same tool in two DIFFERENT turns hasn't
        repeated anything within one turn — see the function's own
        docstring."""
        same_call = AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"})))
        messages = [
            HumanMessage(content="turn one"),
            same_call,
            HumanMessage(content="turn two"),  # a NEW turn starts here
            same_call,
        ]
        assert _consecutive_repeat_count(messages) == 1


class TestNoProgressDetection:
    def _repeating_state(self, count):
        call = AIMessage(content="", tool_calls=_tc_batch(("calculator", {"expression": "1+1"})))
        return {
            "messages": [HumanMessage(content="hi")] + [call] * count,
            "iterations": count,
        }

    def test_ends_the_turn_once_max_repeated_actions_is_reached(self):
        assert should_continue(self._repeating_state(MAX_REPEATED_ACTIONS)) == "__end__"

    def test_does_not_end_below_the_threshold(self):
        assert should_continue(self._repeating_state(MAX_REPEATED_ACTIONS - 1)) == "tools"


class TestRouteAfterModeration:
    def test_blocked_routes_to_reject_moderation(self):
        assert route_after_moderation({"moderation_blocked": True}) == "reject_moderation"

    def test_allowed_routes_to_check_semantic_cache(self):
        assert route_after_moderation({"moderation_blocked": False}) == "check_semantic_cache"

    def test_absent_flag_defaults_to_allowed(self):
        assert route_after_moderation({}) == "check_semantic_cache"


class TestRouteAfterCompaction:
    """AR-015a's edge case (app/graph.py's route_after_compaction) — a
    history_summary still over MAX_HISTORY_SUMMARY_CHARS after
    compact_history just updated it ends the turn instead of feeding a
    runaway summary into agent() on every future call."""

    def test_within_budget_proceeds_to_moderation(self):
        assert route_after_compaction({"history_summary": "short"}) == "moderate_input"

    def test_absent_summary_proceeds_to_moderation(self):
        assert route_after_compaction({}) == "moderate_input"

    def test_over_budget_ends_at_context_window_exceeded(self):
        state = {"history_summary": "x" * (MAX_HISTORY_SUMMARY_CHARS + 1)}
        assert route_after_compaction(state) == "context_window_exceeded"

    def test_at_exactly_the_limit_still_proceeds(self):
        state = {"history_summary": "x" * MAX_HISTORY_SUMMARY_CHARS}
        assert route_after_compaction(state) == "moderate_input"


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

    def test_cancelled_routes_straight_to_end_never_agent(self):
        """A cancellation is a caller-initiated abort, not feedback for
        another attempt — it must never loop back to `agent` the way a
        rejection does (GRAPH_PATTERNS.md pattern 36)."""
        assert route_after_approval({"cancelled": True, "approved": False}) == "__end__"

    def test_cancelled_wins_even_if_approved_is_somehow_also_true(self):
        """cancelled is checked FIRST — an inconsistent state (both set)
        must still fail toward "stop," never toward "run the tool"."""
        assert route_after_approval({"cancelled": True, "approved": True}) == "__end__"


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
