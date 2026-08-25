"""Tests for app/agent.py::get_session_messages — the session switcher's
transcript replay (item #9). Bypasses the real durable checkpointer via a
monkeypatched init_graph_async (same pattern
tests/test_streaming_terminal_events.py's `_events_for` helper already
uses) since only the message-filtering/normalization logic is under test
here, not checkpointer machinery (covered separately by
tests/test_durable_checkpoint.py).
"""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import agent as agent_module


class _FakeState:
    def __init__(self, values):
        self.values = values


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages

    async def aget_state(self, cfg):
        return _FakeState({"messages": self._messages})


def _get(messages, monkeypatch):
    async def fake_init_graph_async():
        return _FakeGraph(messages)

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
    return asyncio.run(agent_module.get_session_messages("t1"))


def test_human_and_ai_messages_become_user_and_assistant_roles(monkeypatch):
    result = _get(
        [
            HumanMessage(content="What is the refund policy?"),
            AIMessage(content="30 days, no questions asked."),
        ],
        monkeypatch,
    )
    assert result == [
        {"role": "user", "text": "What is the refund policy?"},
        {"role": "assistant", "text": "30 days, no questions asked."},
    ]


def test_system_and_tool_messages_are_omitted(monkeypatch):
    """A replay of the conversational back-and-forth a user saw — not a
    full forensic trace of every SystemMessage/ToolMessage the graph
    internally exchanged (that's what Langfuse is for)."""
    result = _get(
        [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="remember our refund policy"),
            AIMessage(
                content="",
                tool_calls=[{"name": "add_note", "args": {}, "id": "c1"}],
            ),
            ToolMessage(content="Note saved.", tool_call_id="c1"),
            AIMessage(content="I've saved that note for you."),
        ],
        monkeypatch,
    )
    assert result == [
        {"role": "user", "text": "remember our refund policy"},
        {"role": "assistant", "text": "I've saved that note for you."},
    ]


def test_an_ai_message_with_no_text_is_skipped_not_shown_as_an_empty_bubble(monkeypatch):
    """The pure-tool-calling AIMessage above (empty content, only
    tool_calls) is exactly this case — asserted again here in isolation
    so a future reader sees the rule on its own, not just inferred from
    the combined scenario above."""
    result = _get(
        [
            HumanMessage(content="what is 12 * 7?"),
            AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "c1"}]),
            ToolMessage(content="84", tool_call_id="c1"),
            AIMessage(content="12 times 7 is 84."),
        ],
        monkeypatch,
    )
    assert result == [
        {"role": "user", "text": "what is 12 * 7?"},
        {"role": "assistant", "text": "12 times 7 is 84."},
    ]


def test_multimodal_content_list_is_flattened_to_text(monkeypatch):
    """An attached image (GRAPH_PATTERNS.md pattern 44) rides alongside
    text in a list-shaped .content — the replay should show only the
    text part, same normalization _run_graph_stream's token handling
    already applies via the shared _text_content helper."""
    result = _get(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "what's in this image?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            ),
            AIMessage(content="A photo of a cat."),
        ],
        monkeypatch,
    )
    assert result == [
        {"role": "user", "text": "what's in this image?"},
        {"role": "assistant", "text": "A photo of a cat."},
    ]


def test_empty_thread_returns_an_empty_list(monkeypatch):
    assert _get([], monkeypatch) == []
