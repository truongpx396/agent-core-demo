"""Tests for astream_events_turn_unattended's auto-decline handling of an
unexpected pause.

Its callers (app/job_queue/agent_worker.py's queue consumer, app/channels/telegram.py)
have no interactive human on the other end of the call to solicit a real
approval decision from — unlike astream_events_turn's approval_required/
astream_events_resume round trip — so when a turn pauses at human_approval
(opt-in or, since add_note, the mandatory capability gate), it must
auto-decline rather than leave the checkpoint paused forever or silently
run an unreviewed tool call. See app/agent/runtime/core.py's matching docstring.
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.graph.build import GraphDeps
from app.agent.graph.hitl import paused_approval_async
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module
from app.core import metrics
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count
from tests.ecorp import build_graph


class TestAstreamEventsTurnUnattended:
    """astream_events_turn_unattended (GRAPH_PATTERNS.md pattern 43) — the async, streaming sibling of pattern
    8's auto-decline, used by app/job_queue/agent_worker.py's Redis Streams consumer and
    app/channels/telegram.py, neither of which has an interactive human to show an approval_required event to.
    """

    async def test_forwards_every_event_unchanged_when_never_paused(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "token", "content": "hi"}
            yield {"type": "done"}

        monkeypatch.setattr(stream_module, "astream_events_turn", fake_turn)

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        events = await _run()
        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]

    async def test_swallows_approval_required_and_forwards_the_auto_decline_resume(
        self, monkeypatch
    ):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "token", "content": "hi"}
            yield {"type": "approval_required", "tool_calls": []}

        async def fake_resume(thread_id, approved, ctx, *, admitted=False):
            assert approved is False  # auto-DECLINE, never auto-approve
            # A step of an already-admitted request: re-checking the allowance here could
            # refuse the decline and leave the conversation paused (spec 008 A6).
            assert admitted is True
            yield {"type": "done"}

        monkeypatch.setattr(stream_module, "astream_events_turn", fake_turn)
        monkeypatch.setattr(stream_module, "astream_events_resume", fake_resume)
        before = _count(metrics.agent_unattended_pause_total)

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        events = await _run()
        # approval_required itself is swallowed, never forwarded — only the
        # events on either side of it (the token, then the resume's "done").
        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]
        assert _count(metrics.agent_unattended_pause_total) == before + 1

    async def test_never_calls_resume_when_never_paused(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "done"}

        def fail_if_called(*a, **kw):
            raise AssertionError("astream_events_resume should not be called")

        monkeypatch.setattr(stream_module, "astream_events_turn", fake_turn)
        monkeypatch.setattr(stream_module, "astream_events_resume", fail_if_called)

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn_unattended(
                    "q", "t1", TEST_CTX
                )
            ]

        await _run()

    async def test_a_consumer_that_stops_early_closes_every_stream_it_holds_open(self, monkeypatch):
        """The wrapper holds a turn's stream open through `_drain`, and a consumer that stops early
        closes only the outermost generator. Each layer has to close the one it drives, or the
        turn's own cleanup (its trace, its abandoned-turn metrics, its budget hold) waits for the
        event loop's finalizer. Checked straight after `aclose()`, with no loop turn in between."""
        closed = []

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            try:
                yield {"type": "token", "content": "hi"}
                yield {"type": "done"}
            finally:
                closed.append("turn")

        monkeypatch.setattr(stream_module, "astream_events_turn", fake_turn)

        stream = stream_module.astream_events_turn_unattended("q", "t1", TEST_CTX)
        assert (await stream.__anext__())["type"] == "token"
        await stream.aclose()

        assert closed == ["turn"]


class TestAstreamEventsContinueTurnUnattended:
    """`astream_events_continue_turn_unattended` is what the queue worker runs when it retries a crashed
    unattended turn (a Telegram message) after the worker that began it died.
    """

    async def test_forwards_every_event_unchanged_when_never_paused(self, monkeypatch):
        async def fake_continue(thread_id, ctx, cancel_check=None):
            yield {"type": "token", "content": "hi"}
            yield {"type": "done"}

        monkeypatch.setattr(stream_module, "astream_events_continue_turn", fake_continue)

        events = [e async for e in stream_module.astream_events_continue_turn_unattended("t1", TEST_CTX)]

        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]

    async def test_declines_a_pause_the_continuation_reaches_and_never_approves(self, monkeypatch):
        async def fake_continue(thread_id, ctx, cancel_check=None):
            yield {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}

        resumed_with = []

        async def fake_resume(thread_id, approved, ctx, *, admitted=False):
            resumed_with.append((approved, admitted))
            yield {"type": "done"}

        monkeypatch.setattr(stream_module, "astream_events_continue_turn", fake_continue)
        monkeypatch.setattr(stream_module, "astream_events_resume", fake_resume)
        before = _count(metrics.agent_unattended_pause_total)

        events = [e async for e in stream_module.astream_events_continue_turn_unattended("t1", TEST_CTX)]

        # The pause itself is swallowed; only the decline's own events reach the channel.
        assert events == [{"type": "done"}]
        assert resumed_with == [(False, True)]  # declined, and as a step of an already-admitted request
        assert _count(metrics.agent_unattended_pause_total) == before + 1

    async def test_a_model_that_keeps_asking_ends_in_a_cancel_and_an_explanation(self, monkeypatch):
        """The ceiling and the cancel are the shared loop's, not re-implemented per entry point."""

        async def fake_continue(thread_id, ctx, cancel_check=None):
            yield {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}

        async def fake_resume(thread_id, approved, ctx, *, admitted=False):
            yield {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]}

        cancelled = []

        async def fake_cancel_run(thread_id, ctx):
            cancelled.append(thread_id)
            return True

        monkeypatch.setattr(stream_module, "astream_events_continue_turn", fake_continue)
        monkeypatch.setattr(stream_module, "astream_events_resume", fake_resume)
        monkeypatch.setattr(stream_module, "cancel_run", fake_cancel_run)
        monkeypatch.setattr(stream_module, "UNATTENDED_MAX_DECLINE_ROUNDS", 2)

        events = [e async for e in stream_module.astream_events_continue_turn_unattended("t1", TEST_CTX)]

        assert cancelled == ["t1"]  # the conversation is not left paused
        assert [e["type"] for e in events] == ["token", "done"]
        assert "add_note" in events[0]["content"]
        assert "person's approval" in events[0]["content"]

    async def test_a_consumer_that_stops_early_closes_the_continuation_it_holds_open(self, monkeypatch):
        closed = []

        async def fake_continue(thread_id, ctx, cancel_check=None):
            try:
                yield {"type": "token", "content": "hi"}
                yield {"type": "done"}
            finally:
                closed.append("continue")

        monkeypatch.setattr(stream_module, "astream_events_continue_turn", fake_continue)

        stream = stream_module.astream_events_continue_turn_unattended("t1", TEST_CTX)
        assert (await stream.__anext__())["type"] == "token"
        await stream.aclose()

        assert closed == ["continue"]


class _ScriptedModel(BaseChatModel):
    """Returns its scripted messages in order. Deliberately has no streaming
    implementation, so `astream_events` falls back to `ainvoke`:
    GenericFakeChatModel cannot stream an empty-content tool call ("No
    generations found in stream"), which is exactly the shape these tests
    need the model to produce."""

    script: list[Any]
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[self.idx]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _add_note_call(call_id):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "add_note", "args": {"title": "t", "content": "c", "topic": "company"}, "id": call_id}
        ],
    )


class TestUnattendedSecondPause:
    """Real bug (reproduced before this fix): the helper declined ONE pause. If the model then re-requested the
    same gated write, the conversation paused a SECOND time, that `approval_required` was forwarded (not
    declined), and the checkpoint stayed paused.
    """

    @staticmethod
    async def _run(monkeypatch, script, thread_id):
        graph = build_graph(GraphDeps(llm=_ScriptedModel(script=script)))

        async def fake_init_graph_async(*a, **k):
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (None, []))
        events = [
            e async for e in stream_module.astream_events_turn_unattended("please save a note", thread_id, TEST_CTX)
        ]
        return graph, events

    async def test_a_second_pause_is_declined_too_and_the_turn_ends_normally(self, monkeypatch):
        before = _count(metrics.agent_unattended_pause_total)
        graph, events = await self._run(
            monkeypatch,
            [
                _add_note_call("c1"),
                _add_note_call("c2"),  # the model re-requests after the first decline
                AIMessage(content="I couldn't save that note, but here is a complete answer instead."),
            ],
            "unattended-1",
        )

        cfg = {"configurable": {"thread_id": "unattended-1"}}
        assert await paused_approval_async(graph, cfg) is None, "the thread was left paused"
        assert [e["type"] for e in events if e["type"] in ("approval_required", "done", "error")] == ["done"]
        assert "complete answer" in "".join(e["content"] for e in events if e["type"] == "token")
        assert _count(metrics.agent_unattended_pause_total) == before + 2  # one per decline

        messages = (await graph.aget_state(cfg)).values["messages"]
        tool_results = [m.content for m in messages if isinstance(m, ToolMessage)]
        assert tool_results and all("added" not in r for r in tool_results), "a declined write must never run"

    async def test_a_model_that_keeps_requesting_the_write_is_cancelled_with_an_explicit_message(
        self, monkeypatch
    ):
        monkeypatch.setattr(stream_module, "UNATTENDED_MAX_DECLINE_ROUNDS", 1)
        graph, events = await self._run(
            monkeypatch,
            [_add_note_call("c1"), _add_note_call("c2"), _add_note_call("c3")],
            "unattended-2",
        )

        cfg = {"configurable": {"thread_id": "unattended-2"}}
        assert await paused_approval_async(graph, cfg) is None, "the thread was left paused"
        terminal = [e["type"] for e in events if e["type"] in ("approval_required", "done", "error")]
        assert terminal == ["done"], "exactly one terminal event, and never a dangling approval_required"
        text = "".join(e["content"] for e in events if e["type"] == "token")
        assert "approval" in text.lower() and "add_note" in text, f"no explicit explanation: {text!r}"
        messages = (await graph.aget_state(cfg)).values["messages"]
        assert not any(isinstance(m, ToolMessage) and "added" in m.content for m in messages)
