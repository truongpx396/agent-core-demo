"""Tests for answer()/stream_turn()'s handling of an unexpected pause.

These two entry points (POST /chat and the CLI's plain `make chat` mode)
have no way to solicit a real human approval decision — unlike
astream_events_turn's approval_required/astream_events_resume round trip —
so when a turn pauses at human_approval (opt-in or, since add_note, the
mandatory capability gate), they must auto-decline rather than leave the
checkpoint paused forever or silently run an unreviewed tool call. See
app/agent.py's matching comments on both functions.

`get_graph()` is monkeypatched to a hermetic fake-LLM-backed graph (a
MemorySaver checkpointer, not the real durable one — that machinery is
already covered by tests/test_durable_checkpoint.py) so these stay fast
and offline, same as the rest of the suite.
"""
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent as agent_module
from app import metrics
from app.graph import GraphDeps, build_graph
from tests.conftest import TEST_CTX


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def _install_fake_graph(monkeypatch, *responses):
    """Point app.agent.get_graph() at a hermetic graph so answer()/
    stream_turn() exercise their real pause-handling logic without a live
    LLM/Qdrant, and without touching the durable checkpointer machinery
    (a different concern, covered elsewhere)."""
    llm = GenericFakeChatModel(messages=iter(responses))
    graph = build_graph(GraphDeps(llm=llm))
    monkeypatch.setattr(agent_module, "get_graph", lambda: graph)
    return graph


class TestAnswerAutoDecline:
    def test_mutating_tool_call_is_auto_declined_and_still_returns_an_answer(
        self, monkeypatch
    ):
        _install_fake_graph(
            monkeypatch,
            _tool_call_message(
                "add_note", {"title": "T", "content": "C", "topic": "company"}
            ),
            AIMessage(content="Okay, I won't save that without approval."),
        )
        before = _count(metrics.agent_unattended_pause_total)

        result = agent_module.answer("remember this", str(uuid.uuid4()), TEST_CTX)

        assert result == "Okay, I won't save that without approval."
        assert _count(metrics.agent_unattended_pause_total) == before + 1

    def test_read_only_tool_call_never_pauses_or_auto_declines(self, monkeypatch):
        """Regression guard: answer() must not treat every tool call as a
        pause — only ones that actually reach human_approval."""
        _install_fake_graph(
            monkeypatch,
            _tool_call_message("calculator", {"expression": "1+1"}),
            AIMessage(content="1 plus 1 equals 2, a proper final answer."),
        )
        before = _count(metrics.agent_unattended_pause_total)

        result = agent_module.answer("what is 1+1", str(uuid.uuid4()), TEST_CTX)

        assert "equals 2" in result
        assert _count(metrics.agent_unattended_pause_total) == before


class TestStreamTurnAutoDecline:
    def test_mutating_tool_call_is_auto_declined_and_still_streams_an_answer(
        self, monkeypatch
    ):
        _install_fake_graph(
            monkeypatch,
            _tool_call_message(
                "add_note", {"title": "T", "content": "C", "topic": "company"}
            ),
            AIMessage(content="Okay, I won't save that without approval."),
        )
        before = _count(metrics.agent_unattended_pause_total)

        chunks = list(agent_module.stream_turn("remember this", str(uuid.uuid4()), TEST_CTX))

        assert "".join(chunks) == "Okay, I won't save that without approval."
        assert _count(metrics.agent_unattended_pause_total) == before + 1

    def test_read_only_tool_call_never_pauses_or_auto_declines(self, monkeypatch):
        _install_fake_graph(
            monkeypatch,
            _tool_call_message("calculator", {"expression": "1+1"}),
            AIMessage(content="1 plus 1 equals 2, a proper final answer."),
        )
        before = _count(metrics.agent_unattended_pause_total)

        chunks = list(agent_module.stream_turn("what is 1+1", str(uuid.uuid4()), TEST_CTX))

        assert "equals 2" in "".join(chunks)
        assert _count(metrics.agent_unattended_pause_total) == before
