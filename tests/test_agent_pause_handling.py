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
import asyncio
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent as agent_module
from app import metrics
from app.errors import ErrorCode
from app.graph import GraphDeps, build_graph
from tests.conftest import TEST_CTX


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def _install_fake_graph(monkeypatch, *responses, search_docs=None):
    """Point app.agent.get_graph() at a hermetic graph so answer()/
    stream_turn() exercise their real pause-handling logic without a live
    LLM/Qdrant, and without touching the durable checkpointer machinery
    (a different concern, covered elsewhere)."""
    llm = GenericFakeChatModel(messages=iter(responses))
    deps = GraphDeps(llm=llm, search_docs=search_docs) if search_docs else GraphDeps(llm=llm)
    graph = build_graph(deps)
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

        result, citations, error, ungrounded = agent_module.answer("remember this", str(uuid.uuid4()), TEST_CTX)

        assert result == "Okay, I won't save that without approval."
        assert citations == []
        assert error is None  # a successful auto-decline is not an error (pattern 30)
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

        result, citations, error, ungrounded = agent_module.answer("what is 1+1", str(uuid.uuid4()), TEST_CTX)

        assert "equals 2" in result
        assert citations == []
        assert error is None
        assert _count(metrics.agent_unattended_pause_total) == before

    def test_returns_only_the_citations_the_answer_actually_used(self, monkeypatch):
        cited = {
            "marker": "[1]",
            "doc_id": "abc123",
            "title": "Checkpointers",
            "text": "Checkpointers persist state.",
            "score": 0.9,
        }
        uncited = {**cited, "marker": "[2]", "doc_id": "def456"}

        def fake_search_docs(query, ctx):
            return "[1] ...\n[2] ...", [cited, uncited]

        _install_fake_graph(
            monkeypatch,
            AIMessage(content="Checkpointers persist state [1]."),
            search_docs=fake_search_docs,
        )

        result, citations, error, ungrounded = agent_module.answer(
            "what is a checkpointer?", str(uuid.uuid4()), TEST_CTX
        )

        assert result == "Checkpointers persist state [1]."
        assert error is None
        assert citations == [cited]


class TestAnswerErrorEnvelope:
    """answer()'s third return value (GRAPH_PATTERNS.md pattern 30) — set
    only for a genuine failure (timeout, a stale/incompatible checkpoint),
    never for a successful auto-decline (see TestAnswerAutoDecline above,
    which already asserts `error is None` on that path)."""

    def test_timeout_returns_a_timeout_envelope(self, monkeypatch):
        _install_fake_graph(monkeypatch, AIMessage(content="unused"))
        monkeypatch.setattr(agent_module, "_invoke_with_timeout", lambda *a, **kw: None)

        result, citations, error, ungrounded = agent_module.answer(
            "hello", str(uuid.uuid4()), TEST_CTX
        )

        assert citations == []
        assert error is not None
        assert error.code == ErrorCode.TIMEOUT
        assert result == error.message

    def test_stale_checkpoint_returns_a_checkpoint_incompatible_envelope(
        self, monkeypatch
    ):
        _install_fake_graph(
            monkeypatch,
            _tool_call_message(
                "add_note", {"title": "T", "content": "C", "topic": "company"}
            ),
        )
        monkeypatch.setattr(
            agent_module,
            "resumability_error",
            lambda graph, cfg: "checkpoint_incompatible: paused under an old schema",
        )

        result, citations, error, ungrounded = agent_module.answer(
            "remember this", str(uuid.uuid4()), TEST_CTX
        )

        assert citations == []
        assert error is not None
        assert error.code == ErrorCode.CHECKPOINT_INCOMPATIBLE
        assert "checkpoint_incompatible" in result

    def test_lost_checkpoint_returns_a_checkpoint_lost_envelope(self, monkeypatch):
        _install_fake_graph(
            monkeypatch,
            _tool_call_message(
                "add_note", {"title": "T", "content": "C", "topic": "company"}
            ),
        )
        monkeypatch.setattr(
            agent_module,
            "resumability_error",
            lambda graph, cfg: "checkpoint_lost: no paused run found",
        )

        _, _, error, _ = agent_module.answer("remember this", str(uuid.uuid4()), TEST_CTX)

        assert error.code == ErrorCode.CHECKPOINT_LOST


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


class TestAstreamEventsTurnUnattended:
    """astream_events_turn_unattended (GRAPH_PATTERNS.md pattern 43) — the
    async, streaming sibling of answer()/stream_turn()'s auto-decline
    (pattern 8), used by app/agent_worker.py's Redis Streams consumer,
    which has no interactive human to show an approval_required event to.
    Tested by monkeypatching astream_events_turn/astream_events_resume
    themselves (both plain module-level functions) rather than driving a
    real graph — the event-forwarding logic is what's under test here, not
    the graph/checkpointer machinery those two already have their own
    coverage for (tests/test_durable_checkpoint.py)."""

    def test_forwards_every_event_unchanged_when_never_paused(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "token", "content": "hi"}
            yield {"type": "done"}

        monkeypatch.setattr(agent_module, "astream_events_turn", fake_turn)

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        events = asyncio.run(_run())
        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]

    def test_swallows_approval_required_and_forwards_the_auto_decline_resume(
        self, monkeypatch
    ):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "token", "content": "hi"}
            yield {"type": "approval_required", "tool_calls": []}

        async def fake_resume(thread_id, approved, ctx):
            assert approved is False  # auto-DECLINE, never auto-approve
            yield {"type": "done"}

        monkeypatch.setattr(agent_module, "astream_events_turn", fake_turn)
        monkeypatch.setattr(agent_module, "astream_events_resume", fake_resume)
        before = _count(metrics.agent_unattended_pause_total)

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        events = asyncio.run(_run())
        # approval_required itself is swallowed, never forwarded — only the
        # events on either side of it (the token, then the resume's "done").
        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]
        assert _count(metrics.agent_unattended_pause_total) == before + 1

    def test_never_calls_resume_when_never_paused(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "done"}

        def fail_if_called(*a, **kw):
            raise AssertionError("astream_events_resume should not be called")

        monkeypatch.setattr(agent_module, "astream_events_turn", fake_turn)
        monkeypatch.setattr(agent_module, "astream_events_resume", fail_if_called)

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        asyncio.run(_run())
