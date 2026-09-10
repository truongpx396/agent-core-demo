"""Tests for the multi-layer safety budgets: per-turn iteration/token
counters actually reset between turns, the tool-call-per-turn cap, the
token-per-turn cap, and the tool execution timeout.

Request-level wall-clock timeout (REQUEST_TIMEOUT_SECONDS, app/agent/runtime.py)
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

from app.agent import runtime as agent_module
from app.agent import tools
from app.agent.graph import (
    COMPACTION_MARKER_KEY,
    MAX_ITERATIONS,
    MAX_SUBAGENT_TOKENS_PER_RUN,
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    _estimate_tokens,
    _messages_to_trim,
    _trim_history,
    make_compact_history_node,
    validate_input,
)
from app.agent.graph_routing import should_continue
from app.agent.graph_tools import too_many_tool_calls
from app.core import metrics
from app.core.config import MAX_COST_USD_PER_TURN, MAX_SUBAGENT_COST_USD_PER_RUN
from tests.conftest import TEST_CTX, metric_value


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
            "subagent_spend": [(500, 0.05)],
        }
        result = validate_input(state, _cfg())
        assert result["iterations"] == 0
        assert result["total_tokens"] == 0
        assert result["subagent_spend"] == []

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
        boundary (app/api/main.py's header extraction, or a local dev ctx) —
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
    across a long-running thread — see HISTORY_TOKEN_CEILING/FLOOR and
    _trim_history in app/agent/graph.py.

    Uses small, LOCAL ceiling/floor values throughout (never the real
    production constants, which would need thousands of tokens of
    placeholder content to exercise) computed via the real tiktoken-backed
    _estimate_tokens against real, short content — these exercise the
    actual estimation path rather than a hand-guessed token count that
    could silently drift out of sync with the real encoding.
    """

    @staticmethod
    def _turn(i):
        return [
            HumanMessage(content=f"question number {i} with some real words in it", id=f"h{i}"),
            AIMessage(content=f"answer number {i} with some real words in it too", id=f"a{i}"),
        ]

    @classmethod
    def _turns(cls, n):
        messages = [SystemMessage(content="seed", id="sys")]
        for i in range(n):
            messages.extend(cls._turn(i))
        return messages

    @staticmethod
    def _non_system(messages):
        return [m for m in messages if not isinstance(m, SystemMessage)]

    def test_within_budget_trims_nothing(self):
        messages = self._turns(3)
        total = _estimate_tokens(self._non_system(messages))
        assert _trim_history(messages, ceiling=total, floor=total) == []

    def test_over_budget_drops_oldest_whole_turns_only(self):
        messages = self._turns(5)
        # messages layout: [sys, h0,a0, h1,a1, h2,a2, h3,a3, h4,a4] — index 5 is h2.
        kept_tail = self._non_system(messages[5:])
        ceiling = _estimate_tokens(self._non_system(messages)) - 1  # trips immediately
        floor = _estimate_tokens(kept_tail)  # fits exactly the last 3 turns
        removed = _trim_history(messages, ceiling=ceiling, floor=floor)
        removed_ids = {rm.id for rm in removed}
        assert all(isinstance(rm, RemoveMessage) for rm in removed)

        # The two oldest turns (h0/a0, h1/a1) are dropped whole ...
        assert removed_ids == {"h0", "a0", "h1", "a1"}
        # ... the seeded system message is never touched ...
        assert "sys" not in removed_ids

    def test_message_without_id_is_left_alone(self):
        """RemoveMessage deletes by id — a message with none (only possible
        outside a compiled graph) can't be targeted, so it must not crash
        or be silently mismatched to the wrong message."""
        messages = self._turns(3)
        messages[1].id = None  # the oldest turn's HumanMessage
        ceiling = _estimate_tokens(self._non_system(messages)) - 1
        removed = _trim_history(messages, ceiling=ceiling, floor=1)
        assert None not in {rm.id for rm in removed}

    def test_never_drops_the_most_recent_turn(self):
        """Even a floor so tight that keeping just the last turn alone
        still exceeds it must not drop the current turn itself."""
        messages = self._turns(3)
        ceiling = _estimate_tokens(self._non_system(messages)) - 1
        removed = _trim_history(messages, ceiling=ceiling, floor=1)
        removed_ids = {rm.id for rm in removed}
        assert "h2" not in removed_ids
        assert "a2" not in removed_ids

    def test_hysteresis_leaves_no_immediate_retrigger(self):
        """The actual point of the ceiling/floor gap: right after one
        compaction, the KEPT tail must already sit at/under the ceiling
        too — otherwise the very next turn's growth would trip compaction
        again immediately, the sliding-window-of-1 behavior this hysteresis
        design replaces (verified empirically against the prior turn-count
        design: it re-triggered on every single turn once past threshold)."""
        messages = self._turns(6)
        ceiling = _estimate_tokens(self._non_system(messages)) - 1  # trips now
        floor = _estimate_tokens(self._non_system(messages[-4:]))  # ~last 2 turns

        trimmed = _messages_to_trim(messages, ceiling=ceiling, floor=floor)
        trimmed_ids = {m.id for m in trimmed}
        survivors = [m for m in messages if m.id not in trimmed_ids]

        assert _estimate_tokens(self._non_system(survivors)) <= ceiling
        # Compacting again immediately, on the SAME (already-trimmed)
        # messages, must be a no-op — real headroom exists before the next
        # turn's own growth could retrigger it.
        assert _messages_to_trim(survivors, ceiling=ceiling, floor=floor) == []

    def test_validate_input_no_longer_touches_messages(self):
        """Trimming/summarization moved to compact_history (see below) —
        validate_input stays a plain, dependency-free function of
        state/config with no LLM call of its own."""
        state = {"messages": self._turns(3)}
        result = validate_input(state, _cfg())
        assert "messages" not in result


class TestCompactHistoryNode:
    """compact_history (app/agent/graph.py) replaced validate_input's old
    discard-only trim with a discard-AND-summarize node — see
    _messages_to_trim (the shared "what falls outside the window" helper)
    and make_compact_history_node's docstring.

    Every test here passes small, LOCAL ceiling/floor overrides to
    make_compact_history_node — never the real HISTORY_TOKEN_CEILING/FLOOR
    production constants, which would need thousands of tokens of
    placeholder content to actually trip."""

    @staticmethod
    def _turns(n):
        messages = [SystemMessage(content="seed", id="sys")]
        for i in range(n):
            messages.append(
                HumanMessage(content=f"question number {i} with some real words", id=f"h{i}")
            )
            messages.append(
                AIMessage(content=f"answer number {i} with some real words too", id=f"a{i}")
            )
        return messages

    @staticmethod
    def _tripped_ceiling(messages):
        """A ceiling guaranteed to already be exceeded by `messages`."""
        return _estimate_tokens([m for m in messages if not isinstance(m, SystemMessage)]) - 1

    def test_applies_the_trim_and_increments_metric(self):
        before = metric_value(metrics.agent_history_compacted_total)
        messages = self._turns(3)
        compact_history = make_compact_history_node(
            GenericFakeChatModel(messages=iter([AIMessage(content="a summary")])),
            ceiling=self._tripped_ceiling(messages),
            floor=1,
        )
        result = compact_history({"messages": messages})

        assert "messages" in result
        *removals, marker = result["messages"]
        assert all(isinstance(m, RemoveMessage) for m in removals)
        # The permanent breadcrumb (see COMPACTION_MARKER_KEY's own
        # docstring) — a real, tagged SystemMessage, not another removal,
        # appended last so a transcript replay sees it after the turns it
        # describes.
        assert isinstance(marker, SystemMessage)
        assert marker.additional_kwargs.get(COMPACTION_MARKER_KEY) is True
        assert "summarized" in marker.content
        assert result["history_summary"] == "a summary"
        assert metric_value(metrics.agent_history_compacted_total) == before + 1

    def test_returns_nothing_when_within_budget(self):
        messages = self._turns(1)
        compact_history = make_compact_history_node(
            GenericFakeChatModel(messages=iter([])),
            ceiling=_estimate_tokens(messages) + 10,  # comfortably above
            floor=1,
        )
        assert compact_history({"messages": messages}) == {}

    def test_degrades_to_trimming_without_a_summary_on_llm_failure(self):
        class _BoomLLM:
            def invoke(self, messages):
                raise RuntimeError("boom")

        messages = self._turns(3)
        compact_history = make_compact_history_node(
            _BoomLLM(), ceiling=self._tripped_ceiling(messages), floor=1
        )
        result = compact_history({"messages": messages})

        assert "messages" in result
        *removals, marker = result["messages"]
        assert all(isinstance(m, RemoveMessage) for m in removals)
        # Still gets a breadcrumb even when summarization itself failed —
        # worded to say "dropped," not "summarized," since there's no
        # history_summary update to point to.
        assert isinstance(marker, SystemMessage)
        assert marker.additional_kwargs.get(COMPACTION_MARKER_KEY) is True
        assert "dropped" in marker.content
        assert "history_summary" not in result

    def test_extends_a_prior_summary_rather_than_replacing_it(self):
        captured = {}

        class _RecordingLLM:
            def invoke(self, messages):
                captured["prompt"] = messages[0].content
                return AIMessage(content="an extended summary")

        messages = self._turns(3)
        compact_history = make_compact_history_node(
            _RecordingLLM(), ceiling=self._tripped_ceiling(messages), floor=1
        )
        state = {"messages": messages, "history_summary": "earlier summary text"}
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
        assert should_continue(state) == "no_answer"

    def test_missing_total_tokens_defaults_to_zero(self):
        state = {"iterations": 1, "messages": [AIMessage(content="final answer here.")]}
        assert should_continue(state) == "check_output"


class TestSubagentSpendBudget:
    """subagent_spend folds a turn's run_subagent delegations into the
    PARENT's own live token/cost ceiling (GRAPH_PATTERNS.md pattern 46's
    disclosed gap) without touching the nested run's own, separate
    MAX_SUBAGENT_TOKENS_PER_RUN/MAX_SUBAGENT_COST_USD_PER_RUN ceiling."""

    def test_own_tokens_under_budget_but_subagent_spend_tips_it_over(self):
        state = {
            "iterations": 1,
            "total_tokens": MAX_TOKENS_PER_TURN - 1,
            "subagent_spend": [(1, 0.0)],
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "no_answer"

    def test_own_cost_under_budget_but_subagent_spend_tips_it_over(self):
        state = {
            "iterations": 1,
            "total_cost_usd": MAX_COST_USD_PER_TURN - 0.001,
            "subagent_spend": [(0, 0.001)],
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "no_answer"

    def test_missing_subagent_spend_defaults_to_empty(self):
        state = {
            "iterations": 1,
            "total_tokens": MAX_TOKENS_PER_TURN - 1,
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "check_output"

    def test_subagent_spend_well_under_its_own_run_ceiling_can_still_trip_the_turn_ceiling(self):
        """The nested run's own MAX_SUBAGENT_TOKENS_PER_RUN/
        MAX_SUBAGENT_COST_USD_PER_RUN stay independent, per-call ceilings —
        a single subagent call comfortably under ITS ceiling can still be
        the delta that pushes the PARENT turn's own, separate ceiling over,
        once combined with what the turn already spent directly."""
        state = {
            "iterations": 1,
            "total_tokens": MAX_TOKENS_PER_TURN - 1,
            "subagent_spend": [(MAX_SUBAGENT_TOKENS_PER_RUN // 2, 0.0)],
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "no_answer"
        state = {
            "iterations": 1,
            "total_cost_usd": MAX_COST_USD_PER_TURN - 0.001,
            "subagent_spend": [(0, MAX_SUBAGENT_COST_USD_PER_RUN / 2)],
            "messages": [AIMessage(content="final answer, long enough.")],
        }
        assert should_continue(state) == "no_answer"


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


class TestRecursionLimit:
    """app/agent/runtime.py::RECURSION_LIMIT — LangGraph's own graph-step
    cap, a coarser unit than MAX_ITERATIONS. Regression coverage for a real
    bug found while smoke-testing GRAPH_PATTERNS.md pattern 46 against the
    live stack: a flat "12" (this constant's value before the derivation
    below existed) undercounts the real step cost of a full-length agent
    loop — ~5 fixed pre-loop nodes, ~2 more post-loop, plus 2 steps per
    agent<->tools round trip — and trips GraphRecursionError before
    MAX_ITERATIONS ever does, for a real model making several genuine
    tool-call round trips in one turn."""

    def test_comfortably_covers_a_full_length_agent_loop(self):
        # Fixed pre/post-loop nodes (~7) + 2 steps per iteration, with
        # margin — the exact shape a genuinely converging MAX_ITERATIONS-
        # length run costs in real LangGraph steps.
        minimum_needed = 7 + MAX_ITERATIONS * 2
        assert agent_module.RECURSION_LIMIT >= minimum_needed

    def test_derived_from_max_iterations_not_a_bare_literal(self):
        """A future change to MAX_ITERATIONS must move this WITH it —
        the original bug was exactly this staying a hardcoded "12" while
        nothing kept it in sync with the actual loop length."""
        assert agent_module.RECURSION_LIMIT == MAX_ITERATIONS * 2 + 15
