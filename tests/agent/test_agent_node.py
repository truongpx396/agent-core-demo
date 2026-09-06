"""Tests for the `agent` node in isolation, using a fake chat model instead
of the real ChatOpenAI client — see `make_agent_node`'s docstring in
app/agent/graph.py for why it's a factory."""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph import make_agent_node


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


def test_agent_inserts_context_before_the_question_at_the_anchor():
    """context_anchor_index (set once by retrieve_context) says WHERE to
    splice context in — right before the turn's own question, not appended
    after whatever's currently last."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    state = {
        "messages": [question],
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    agent(state)

    assert [type(m).__name__ for m in fake_llm.seen_messages] == [
        "SystemMessage",
        "HumanMessage",
    ]
    assert fake_llm.seen_messages[1] is question


def test_agent_keeps_context_anchored_across_a_turns_own_tool_loop():
    """The whole point of the fix: on a LATER call within the same turn
    (more messages have since piled up after the anchor — a tool round, or
    a retry_output nudge), context must still land at the SAME anchor
    index, immediately before the original question, not at the new
    (shifted) tail — otherwise every call in a multi-round turn sends a
    DIFFERENT prefix and nothing about it stays cacheable."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    later_messages = [
        AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {}, "id": "1"}]),
        HumanMessage(content="That answer was too short — please give a fuller answer."),
    ]
    state = {
        "messages": [question, *later_messages],
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,  # still the original question's index
    }
    agent(state)

    seen = fake_llm.seen_messages
    assert isinstance(seen[0], SystemMessage)
    assert "checkpointers persist" in seen[0].content
    assert seen[1] is question
    assert seen[2:] == later_messages


def test_agent_falls_back_to_appending_context_without_an_anchor():
    """A hand-built State missing context_anchor_index (never ran through
    retrieve_context) must not crash — falls back to the old tail-append."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    state = {"messages": [question], "context": "doc: checkpointers persist graph state."}
    agent(state)

    assert fake_llm.seen_messages[0] is question
    assert isinstance(fake_llm.seen_messages[1], SystemMessage)


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


def test_history_summary_injection_tells_the_model_not_to_restate_it_verbatim():
    """Guardrail against a small model regurgitating its injected summary
    back into the final answer instead of treating it as background-only
    reference (observed against a real qwen2.5:3b deployment: a later
    question's answer dumped an earlier turn's summary/facts/citations all
    into one blob). The anti-regurgitation instruction lives in its OWN
    short, separate reminder message, positioned AFTER (closer to
    generation than) the bulk summary text — not baked into the summary
    message itself — so the summary's bulky content can sit in the stable,
    cache-friendly anchored prefix while only this one short line stays
    recency-weighted. See make_agent_node."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what did we discuss earlier?")],
        "history_summary": "Earlier, the user asked about refund policy.",
    }
    agent(state)

    seen = fake_llm.seen_messages
    summary_idx = next(
        i for i, m in enumerate(seen) if isinstance(m, SystemMessage) and "refund policy" in m.content
    )
    reminder_idx = next(
        i for i, m in enumerate(seen) if isinstance(m, SystemMessage) and "do not restate" in m.content
    )
    assert "do not restate" not in seen[summary_idx].content, (
        "the instruction should live in its own message, not the summary text"
    )
    assert reminder_idx > summary_idx, "the reminder must stay closer to generation than the summary"


def test_agent_anchors_history_summary_before_the_question_same_as_context():
    """The bulk summary TEXT is front-loaded at the anchor (before the
    question), same as context — only the short anti-regurgitation
    reminder stays tail-appended. Summary comes before context: more
    general background first, the currently-relevant retrieved docs
    closest to the question."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what did we discuss earlier?")
    state = {
        "messages": [question],
        "history_summary": "Earlier, the user asked about refund policy.",
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    agent(state)

    seen = fake_llm.seen_messages
    assert "refund policy" in seen[0].content
    assert "checkpointers persist" in seen[1].content
    assert seen[2] is question
    assert "do not restate" in seen[3].content


def test_agent_keeps_history_summary_anchored_across_a_turns_own_tool_loop():
    """Same guarantee as context's own anchor test: on a later call in the
    same turn, the summary TEXT must still land before the original
    question rather than after newly accumulated tool/retry messages —
    only the short reminder trails behind those."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what did we discuss earlier?")
    later_messages = [
        AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {}, "id": "1"}]),
        HumanMessage(content="That answer was too short — please give a fuller answer."),
    ]
    state = {
        "messages": [question, *later_messages],
        "history_summary": "Earlier, the user asked about refund policy.",
        "context_anchor_index": 0,
    }
    agent(state)

    seen = fake_llm.seen_messages
    assert "refund policy" in seen[0].content
    assert seen[1] is question
    assert seen[2:4] == later_messages
    assert "do not restate" in seen[4].content


def test_agent_skips_history_summary_message_when_absent():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    agent({"messages": [HumanMessage(content="hi")]})

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)
