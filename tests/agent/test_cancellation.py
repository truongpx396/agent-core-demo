"""Tests for cooperative mid-turn cancellation (app/agent/runtime.py's
`cancel_check` parameter on `_iterate_with_timeout`/`_run_graph_stream`/
`astream_events_turn`) — the "actively streaming, not yet paused" half of
`POST /chat/cancel`'s two-mechanism design (GRAPH_PATTERNS.md pattern 43's
queue-first HITL). The other half — cancelling a turn already paused at
human_approval, via `cancel_run` — is already covered by
tests/agent/test_durable_checkpoint.py::TestAsyncSeeding; this file is the new
mechanism only.

Follows tests/agent/test_streaming_terminal_events.py's established pattern:
drive `astream_events_turn` against a hermetic fake-LLM graph via a
monkeypatched `init_graph_async`, bypassing the real durable checkpointer
(a different concern, covered elsewhere).
"""
import asyncio
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agent import runtime as agent_module
from app.agent.graph import GraphDeps, build_graph
from app.core import metrics
from app.core.errors import ErrorCode
from tests.conftest import TEST_CTX


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


class TestIterateWithTimeoutCancelCheck:
    """Unit-level: the wrapper itself, no graph involved."""

    def test_raises_turn_cancelled_when_cancel_check_returns_true(self):
        async def aiter():
            yield 1
            yield 2

        async def always_cancelled():
            return True

        async def _run():
            events = []
            raised = None
            try:
                async for e in agent_module._iterate_with_timeout(
                    aiter(), 60, cancel_check=always_cancelled
                ):
                    events.append(e)
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                raised = exc
            return events, raised

        events, raised = asyncio.run(_run())
        assert events == []
        assert isinstance(raised, agent_module.TurnCancelled)

    def test_no_cancel_check_behaves_exactly_as_before(self):
        """Regression guard: every existing caller passes nothing here —
        must be unaffected by this parameter's addition."""

        async def aiter():
            yield 1
            yield 2

        async def _run():
            return [e async for e in agent_module._iterate_with_timeout(aiter(), 60)]

        assert asyncio.run(_run()) == [1, 2]

    def test_cancel_check_returning_false_does_not_interrupt_iteration(self):
        async def aiter():
            yield "a"
            yield "b"

        async def never_cancelled():
            return False

        async def _run():
            return [
                e
                async for e in agent_module._iterate_with_timeout(
                    aiter(), 60, cancel_check=never_cancelled
                )
            ]

        assert asyncio.run(_run()) == ["a", "b"]


def _fake_llm_multi_token_answer():
    return GenericFakeChatModel(
        messages=iter([AIMessage(content="a longer answer with several tokens in it")])
    )


class TestAstreamEventsTurnCancellation:
    """End-to-end through astream_events_turn/_run_graph_stream: a
    cancel_check flipping True mid-turn must surface as a terminal error
    event carrying ErrorCode.CANCELLED — modeled as a terminal-outcome
    envelope like any other (GRAPH_PATTERNS.md pattern 30), not a separate
    SSE event type — and never reach a normal "done"."""

    def test_cancel_mid_stream_yields_a_cancelled_error_event_not_done(self, monkeypatch):
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        # Cancel on the SECOND check — realistic "already streaming when
        # the user clicks Stop" timing, not "cancelled before anything
        # ever ran."
        seen = {"count": 0}

        async def cancel_after_first_check():
            seen["count"] += 1
            return seen["count"] > 1

        before = _count(metrics.agent_streaming_cancellation_total)

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX, cancel_check=cancel_after_first_check
                )
            ]

        events = asyncio.run(_run())

        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == ErrorCode.CANCELLED.value
        assert not any(e["type"] == "done" for e in events)
        assert _count(metrics.agent_streaming_cancellation_total) == before + 1

    def test_cancel_check_that_never_fires_streams_normally_to_completion(self, monkeypatch):
        """A cancel_check present but always False must not change
        behavior — proves the mechanism is additive, not a new source of
        flakiness for the common (not cancelled) case."""
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        async def never_cancelled():
            return False

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX, cancel_check=never_cancelled
                )
            ]

        events = asyncio.run(_run())
        assert events[-1]["type"] == "done"
        assert not any(e["type"] == "error" for e in events)

    def test_no_cancel_check_streams_normally_to_completion(self, monkeypatch):
        """Regression guard: every existing caller (POST /chat/stream,
        app/channels/chat.py) passes no cancel_check at all — must behave exactly
        as before this feature existed."""
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        async def _run():
            return [
                event
                async for event in agent_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX
                )
            ]

        events = asyncio.run(_run())
        assert events[-1]["type"] == "done"
