"""Tests for the `agent` node in isolation, using a fake chat model instead
of the real ChatOpenAI client — see `make_agent_node`'s docstring in
app/graph.py for why it's a factory."""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.graph import make_agent_node


class _RecordingFakeLLM:
    """Wraps GenericFakeChatModel and remembers every message list it was
    invoked with, so tests can assert on exactly what would be sent to the
    real model. A plain wrapper rather than a subclass because
    GenericFakeChatModel is a Pydantic model and rejects arbitrary instance
    attributes on subclasses."""

    def __init__(self, messages):
        self._inner = GenericFakeChatModel(messages=messages)
        self.seen_messages: list = []

    def invoke(self, messages, *args, **kwargs):
        self.seen_messages = list(messages)
        return self._inner.invoke(messages, *args, **kwargs)


def test_agent_invokes_llm_and_bumps_iterations():
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="42")]))
    agent = make_agent_node(fake_llm)

    state = {"messages": [HumanMessage(content="what is 21*2?")], "iterations": 3}
    result = agent(state)

    assert result["iterations"] == 4
    assert result["messages"][0].content == "42"


def test_agent_defaults_missing_iterations_to_zero_then_one():
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="hi")]))
    agent = make_agent_node(fake_llm)

    result = agent({"messages": [HumanMessage(content="hi")]})
    assert result["iterations"] == 1


def test_agent_injects_context_as_system_message_when_present():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what is a checkpointer?")],
        "context": "doc: checkpointers persist graph state.",
    }
    agent(state)

    assert any(
        isinstance(m, SystemMessage) and "checkpointers persist" in m.content
        for m in fake_llm.seen_messages
    )


def test_agent_skips_context_message_when_context_empty():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    agent({"messages": [HumanMessage(content="hi")], "context": ""})

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)


def test_agent_injects_history_summary_as_system_message_when_present():
    """AR-015a (GRAPH_PATTERNS.md pattern 41) — compact_history's running
    summary reaches the model the same way retrieved context does."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what did we discuss earlier?")],
        "history_summary": "Earlier, the user asked about refund policy.",
    }
    agent(state)

    assert any(
        isinstance(m, SystemMessage) and "refund policy" in m.content
        for m in fake_llm.seen_messages
    )


def test_agent_skips_history_summary_message_when_absent():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    agent({"messages": [HumanMessage(content="hi")]})

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)
