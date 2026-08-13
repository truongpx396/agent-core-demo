"""Full-graph integration tests: each test drives build_graph() end-to-end
with a fake chat model standing in for the real ChatOpenAI client, so these
run with no live services — no LiteLLM, no Ollama, no Qdrant.
`retrieve_context`'s search_docs call defaults to the autouse
`mock_search_docs` fixture in conftest.py (which patches
`graph._default_search`); tool-call scenarios below use `calculator` (pure
Python, no network) rather than `search_docs` so the *actual*
tool-execution step (inside ToolNode, which isn't touched by that stub)
also stays hermetic.

Scenarios mirror the graph's documented flow in GRAPH_PATTERNS.md.
"""
import time
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.graph import (
    AGENT_RETRY_POLICY,
    MAX_ITERATIONS,
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    GraphDeps,
    build_graph,
)


def _config():
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _fake_llm(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


class TestRejectPath:
    def test_empty_input_never_reaches_llm(self):
        llm = _fake_llm()  # would raise StopIteration if invoked
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke({"messages": [HumanMessage(content="")]}, config=_config())
        assert "try again" in result["messages"][-1].content.lower()


class TestDirectAnswerPath:
    def test_final_answer_ends_without_tool_call(self):
        llm = _fake_llm(AIMessage(content="This is a sufficiently long final answer."))
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="what is a checkpointer?")]},
            config=_config(),
        )
        assert (
            result["messages"][-1].content
            == "This is a sufficiently long final answer."
        )
        assert result["iterations"] == 1


class TestToolCallPath:
    def test_tool_call_then_final_answer(self):
        llm = _fake_llm(
            _tool_call_message("calculator", {"expression": "12*7"}),
            AIMessage(content="12 times 7 is 84."),
        )
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="what is 12*7?")]}, config=_config()
        )
        assert result["messages"][-1].content == "12 times 7 is 84."
        assert result["iterations"] == 2


class TestHumanApprovalPath:
    def test_approved_tool_call_runs_and_returns_answer(self):
        llm = _fake_llm(
            _tool_call_message("calculator", {"expression": "2+2"}),
            AIMessage(content="2 plus 2 equals 4."),
        )
        g = build_graph(GraphDeps(llm=llm))
        config = _config()
        g.invoke(
            {
                "messages": [HumanMessage(content="what is 2+2?")],
                "require_approval": True,
            },
            config=config,
        )
        state = g.get_state(config)
        assert state.next, "graph should be paused at the interrupt"

        result = g.invoke(Command(resume=True), config=config)
        assert result["messages"][-1].content == "2 plus 2 equals 4."

    def test_rejected_tool_call_returns_to_agent_without_running_tool(self):
        llm = _fake_llm(
            _tool_call_message("calculator", {"expression": "2+2"}),
            AIMessage(content="Okay, I will not run that calculation."),
        )
        g = build_graph(GraphDeps(llm=llm))
        config = _config()
        g.invoke(
            {
                "messages": [HumanMessage(content="what is 2+2?")],
                "require_approval": True,
            },
            config=config,
        )
        result = g.invoke(Command(resume=False), config=config)
        assert (
            result["messages"][-1].content == "Okay, I will not run that calculation."
        )


class TestIterationCap:
    def test_stops_at_max_iterations_even_if_llm_keeps_calling_tools(self):
        # More tool-call responses than MAX_ITERATIONS allows, to prove the
        # cap — not the model running out of things to say — ends the loop.
        responses = [
            _tool_call_message(
                "calculator", {"expression": "1+1"}, call_id=f"call_{i}"
            )
            for i in range(MAX_ITERATIONS + 5)
        ]
        llm = _fake_llm(*responses)
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="loop forever")]}, config=_config()
        )
        assert result["iterations"] == MAX_ITERATIONS


class TestOutputRetryPath:
    def test_short_answer_triggers_retry_then_succeeds(self):
        llm = _fake_llm(
            AIMessage(content="Yes."),
            AIMessage(content="A properly detailed final answer this time."),
        )
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="is that right?")]}, config=_config()
        )
        assert (
            result["messages"][-1].content
            == "A properly detailed final answer this time."
        )
        assert result["iterations"] == 2


class TestToolErrorRecovery:
    def test_invalid_tool_args_become_a_tool_message_not_a_crash(self):
        """calculator's args_schema rejects a blank expression (see
        CalculatorArgs._not_blank in app/tools.py) — that validation error
        should surface to the agent as a ToolMessage via
        handle_tool_errors, not blow up the run."""
        llm = _fake_llm(
            _tool_call_message("calculator", {"expression": ""}),
            AIMessage(content="I couldn't compute that, sorry about it."),
        )
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="what is nothing?")]}, config=_config()
        )
        assert (
            result["messages"][-1].content == "I couldn't compute that, sorry about it."
        )

    def test_slow_tool_call_times_out_and_recovers(self, monkeypatch):
        """app.tools.TOOL_TIMEOUT_SECONDS bounds how long a single tool call
        can block the graph — a hung call should surface as a friendly
        ToolMessage via handle_tool_errors, same as any other tool error,
        not hang the run."""
        from app import tools as tools_module

        monkeypatch.setattr(tools_module, "TOOL_TIMEOUT_SECONDS", 0.05)

        def slow_calculator_impl(expression):
            time.sleep(0.5)
            return "too slow"

        monkeypatch.setattr(tools_module, "_calculator_impl", slow_calculator_impl)

        llm = _fake_llm(
            _tool_call_message("calculator", {"expression": "1+1"}),
            AIMessage(content="Sorry, that calculation timed out."),
        )
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="what is 1+1?")]}, config=_config()
        )
        assert result["messages"][-1].content == "Sorry, that calculation timed out."


class TestToolCallBudgetPath:
    def test_too_many_tool_calls_are_rejected_then_agent_retries_with_fewer(self):
        too_many = AIMessage(
            content="",
            tool_calls=[
                {"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}
                for i in range(MAX_TOOL_CALLS_PER_TURN + 3)
            ],
        )
        llm = _fake_llm(
            too_many,
            _tool_call_message("calculator", {"expression": "1+1"}),
            AIMessage(content="1 plus 1 equals 2, a proper final answer."),
        )
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="add a bunch of stuff")]},
            config=_config(),
        )
        assert "equals 2" in result["messages"][-1].content


class TestReliabilityPolicy:
    def test_agent_node_has_the_retry_policy_wired(self):
        """Structural check that AGENT_RETRY_POLICY is actually attached to
        the `agent` node in the compiled graph, not just defined and
        unused — a real retry-triggering test would need to sleep through
        RetryPolicy's backoff, which isn't worth the flakiness/runtime for
        what's otherwise a one-line wiring fact."""
        g = build_graph(GraphDeps(llm=_fake_llm(AIMessage(content="anything"))))
        assert g.nodes["agent"].retry_policy == AGENT_RETRY_POLICY

    def test_retrieve_context_and_deterministic_nodes_have_no_retry_policy(self):
        """`retrieve_context` degrades internally instead (see its
        docstring); everything else is a pure function of state where a
        retry would just repeat the same bug — see GRAPH_PATTERNS.md
        pattern 7."""
        g = build_graph(GraphDeps(llm=_fake_llm(AIMessage(content="anything"))))
        for name in ("retrieve_context", "check_output", "too_many_tool_calls"):
            assert g.nodes[name].retry_policy is None


class TestContextRetrievalDegradation:
    def test_search_docs_outage_degrades_instead_of_crashing_the_turn(self):
        """retrieve_context's search_docs call must never take the whole
        turn down with it — see its reliability-policy docstring in
        app/graph.py. The agent still answers, just without pre-fetched
        context (and could still retry search_docs itself as a tool)."""

        def failing_search_docs(query):
            raise RuntimeError("Qdrant unreachable")

        llm = _fake_llm(
            AIMessage(content="A general-knowledge answer, no context needed.")
        )
        g = build_graph(GraphDeps(llm=llm, search_docs=failing_search_docs))
        result = g.invoke(
            {"messages": [HumanMessage(content="what is a checkpointer?")]},
            config=_config(),
        )
        assert (
            result["messages"][-1].content
            == "A general-knowledge answer, no context needed."
        )


class TestTokenBudgetPath:
    def test_token_budget_ends_the_turn_without_running_the_pending_tool_call(self):
        big_usage_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "calculator", "args": {"expression": "1+1"}, "id": "c1"}
            ],
            usage_metadata={
                "input_tokens": MAX_TOKENS_PER_TURN,
                "output_tokens": 0,
                "total_tokens": MAX_TOKENS_PER_TURN,
            },
        )
        llm = _fake_llm(big_usage_msg)
        g = build_graph(GraphDeps(llm=llm))
        result = g.invoke(
            {"messages": [HumanMessage(content="do something expensive")]},
            config=_config(),
        )
        assert result["total_tokens"] >= MAX_TOKENS_PER_TURN
        assert result["iterations"] == 1  # ended after one agent call, tool never ran
