"""Tests for astream_events_turn_unattended's auto-decline handling of an
unexpected pause.

Its callers (app/job_queue/agent_worker.py's queue consumer, app/channels/telegram.py)
have no interactive human on the other end of the call to solicit a real
approval decision from — unlike astream_events_turn's approval_required/
astream_events_resume round trip — so when a turn pauses at human_approval
(opt-in or, since add_note, the mandatory capability gate), it must
auto-decline rather than leave the checkpoint paused forever or silently
run an unreviewed tool call. See app/agent/runtime.py's matching docstring.
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent import runtime as agent_module
from app.agent import runtime_stream as stream_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from app.agent.graph_hitl import paused_approval_async
from app.core import metrics
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count


class TestAstreamEventsTurnUnattended:
    """astream_events_turn_unattended (GRAPH_PATTERNS.md pattern 43) — the
    async, streaming sibling of pattern 8's auto-decline, used by
    app/job_queue/agent_worker.py's Redis Streams consumer and
    app/channels/telegram.py, neither of which has an interactive human to
    show an approval_required event to. Tested by monkeypatching
    astream_events_turn/astream_events_resume themselves (both plain
    module-level functions) rather than driving a real graph — the
    event-forwarding logic is what's under test here, not the
    graph/checkpointer machinery those two already have their own coverage
    for (tests/agent/test_durable_checkpoint.py)."""

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
    """Real bug (reproduced before this fix): the helper declined ONE pause.
    If the model then re-requested the same gated write, the conversation
    paused a SECOND time, that `approval_required` was forwarded (not
    declined), and the checkpoint stayed paused. An unattended channel has
    no way to resolve a pause, so every later message was refused with
    "pending approval" and the user — who had already received an EMPTY reply
    to the first message — could never recover that conversation. The tests
    above mock `astream_events_turn`/`astream_events_resume` and so never
    drove a real graph through a second pause.

    Fails closed throughout: no write ever ran. The defect was liveness, not
    safety."""

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
