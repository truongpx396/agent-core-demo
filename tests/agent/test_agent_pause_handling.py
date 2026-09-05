"""Tests for astream_events_turn_unattended's auto-decline handling of an
unexpected pause.

Its callers (app/turns/agent_worker.py's queue consumer, app/channels/telegram.py)
have no interactive human on the other end of the call to solicit a real
approval decision from — unlike astream_events_turn's approval_required/
astream_events_resume round trip — so when a turn pauses at human_approval
(opt-in or, since add_note, the mandatory capability gate), it must
auto-decline rather than leave the checkpoint paused forever or silently
run an unreviewed tool call. See app/agent/runtime.py's matching docstring.
"""
import asyncio

from app.agent import runtime as agent_module
from app.core import metrics
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count


class TestAstreamEventsTurnUnattended:
    """astream_events_turn_unattended (GRAPH_PATTERNS.md pattern 43) — the
    async, streaming sibling of pattern 8's auto-decline, used by
    app/turns/agent_worker.py's Redis Streams consumer and
    app/channels/telegram.py, neither of which has an interactive human to
    show an approval_required event to. Tested by monkeypatching
    astream_events_turn/astream_events_resume themselves (both plain
    module-level functions) rather than driving a real graph — the
    event-forwarding logic is what's under test here, not the
    graph/checkpointer machinery those two already have their own coverage
    for (tests/agent/test_durable_checkpoint.py)."""

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
