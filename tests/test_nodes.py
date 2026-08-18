"""Node-level tests: each node in isolation, with its dependencies mocked.

No LLM here — that's covered separately in test_agent_node.py, since `agent`
is the only node that needs one.
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import graph, metrics
from tests.conftest import TEST_CTX


def test_reject_input_returns_ai_message_not_human():
    result = graph.reject_input({"messages": []})
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert isinstance(msg, AIMessage)
    assert "question" in msg.content.lower()


def test_reject_context_returns_ai_message_and_increments_metric():
    before = metrics.agent_missing_ctx_total._value.get()
    result = graph.reject_context({"messages": []})
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert isinstance(msg, AIMessage)
    assert "verify" in msg.content.lower()
    assert metrics.agent_missing_ctx_total._value.get() == before + 1


def test_retrieve_context_calls_search_docs_with_last_human_message_and_ctx():
    captured = {}

    def fake_search_docs(query, ctx):
        captured["query"] = query
        captured["ctx"] = ctx
        return "doc 1\ndoc 2"

    retrieve_context = graph.make_retrieve_context_node(fake_search_docs)

    state = {
        "messages": [HumanMessage(content="what is a checkpointer?")],
        "ctx": TEST_CTX,
    }
    result = retrieve_context(state)

    assert captured["query"] == "what is a checkpointer?"
    assert captured["ctx"] == TEST_CTX
    assert result == {"context": "doc 1\ndoc 2"}


def test_retrieve_context_no_human_message_skips_search():
    def fail_search_docs(query, ctx):
        raise AssertionError("search_docs should not be called")

    retrieve_context = graph.make_retrieve_context_node(fail_search_docs)

    result = retrieve_context({"messages": [AIMessage(content="hi")]})
    assert result == {"context": ""}


def test_retrieve_context_degrades_to_empty_when_search_docs_raises():
    """Reliability policy: retrieve_context is enrichment, not the agent's
    only path to this data (the LLM can still call search_docs as a tool),
    so a Qdrant/embedding outage must degrade to no pre-fetched context
    instead of crashing the whole turn — see its docstring in app/graph.py."""

    def failing_search_docs(query, ctx):
        raise RuntimeError("Qdrant unreachable")

    retrieve_context = graph.make_retrieve_context_node(failing_search_docs)
    before = metrics.agent_context_retrieval_degraded_total._value.get()

    state = {
        "messages": [HumanMessage(content="what is a checkpointer?")],
        "ctx": TEST_CTX,
    }
    result = retrieve_context(state)

    assert result == {"context": ""}
    assert metrics.agent_context_retrieval_degraded_total._value.get() == before + 1


def test_check_output_is_a_passthrough():
    assert graph.check_output({"messages": [AIMessage(content="anything")]}) == {}


def test_retry_output_appends_corrective_human_message():
    result = graph.retry_output({"messages": []})
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert isinstance(msg, HumanMessage)
    assert "short" in msg.content.lower()


class TestHumanApproval:
    """`human_approval` calls `interrupt()`, which suspends the whole graph
    run outside of a compiled-graph context — so these node-level tests
    patch it directly. The real pause/resume cycle through a compiled graph
    is covered in test_graph_integration.py."""

    @staticmethod
    def _ai_with_tool_calls(*calls):
        return AIMessage(content="", tool_calls=list(calls))

    def test_approved_sets_approved_flag(self, monkeypatch):
        monkeypatch.setattr(graph, "interrupt", lambda payload: True)
        ai = self._ai_with_tool_calls(
            {"name": "search_docs", "args": {"query": "x"}, "id": "call_1"}
        )
        result = graph.human_approval({"messages": [ai]})
        assert result == {"approved": True}

    def test_rejected_synthesizes_tool_message_per_pending_call(self, monkeypatch):
        """Regression test for the HITL gotcha documented in
        GRAPH_PATTERNS.md: every pending tool_call needs a matching
        ToolMessage, or the next LLM call fails OpenAI's tool-response
        validation."""
        monkeypatch.setattr(graph, "interrupt", lambda payload: False)
        ai = self._ai_with_tool_calls(
            {"name": "search_docs", "args": {"query": "x"}, "id": "call_1"},
            {"name": "calculator", "args": {"expression": "1+1"}, "id": "call_2"},
        )
        result = graph.human_approval({"messages": [ai]})

        assert result["approved"] is False
        assert len(result["messages"]) == 2
        for msg, tc in zip(result["messages"], ai.tool_calls):
            assert isinstance(msg, ToolMessage)
            assert msg.tool_call_id == tc["id"]

    def test_interrupt_payload_contains_tool_call_name_and_args(self, monkeypatch):
        seen = {}

        def fake_interrupt(payload):
            seen.update(payload)
            return True

        monkeypatch.setattr(graph, "interrupt", fake_interrupt)
        ai = self._ai_with_tool_calls(
            {"name": "search_docs", "args": {"query": "x"}, "id": "call_1"}
        )
        graph.human_approval({"messages": [ai]})

        assert seen["action"] == "approve_tool_calls"
        assert seen["tool_calls"] == [{"name": "search_docs", "args": {"query": "x"}}]
