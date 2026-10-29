"""One Langfuse client per process, not one per turn. `_open_trace` built `Langfuse()` every turn and
`_run_graph_stream`'s `finally` built ANOTHER just to `.flush()` it. Each construction starts three background
threads nothing stops (measured on SDK 2.60.10, keys set or not), so a worker gained six threads per turn (60
after ten) and never gave one back (spec 008, B18).

The flush was also wrong: `Langfuse.flush()` joins THAT instance's own queue, so flushing a new client flushed
an empty queue and never the trace's events. A correct flush blocks until the queue drains, so it can't run on
the event loop either: the SDK's background consumer sends events as they arrive, and the shared client is
flushed and shut down once, at process exit.

The leak is checked against the real SDK, since a counting fake can't show threads; behaviour around it uses a
fake.
"""
import logging
import threading
import time

import pytest

from app.agent.runtime import stream as runtime_stream
from app.core import tracing


class _FakeLangfuse:
    constructed = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed += 1
        self.shutdowns = 0
        self.flushes = 0

    def trace(self, **kwargs):
        return _FakeTrace()

    def flush(self):
        self.flushes += 1

    def shutdown(self):
        self.shutdowns += 1


class _FakeTrace:
    def update(self, **kwargs):
        pass


class _FakeCallbackHandler:
    def __init__(self, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _fresh_tracing_state():
    tracing._reset_for_tests()
    _FakeLangfuse.constructed = 0
    yield
    tracing.shutdown_langfuse()
    tracing._reset_for_tests()


@pytest.fixture
def fake_sdk(monkeypatch):
    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", _FakeLangfuse)
    monkeypatch.setattr(runtime_stream, "CallbackHandler", _FakeCallbackHandler)


def test_the_client_is_created_once_and_shared(fake_sdk):
    first = tracing.get_langfuse()
    second = tracing.get_langfuse()

    assert first is second and _FakeLangfuse.constructed == 1


def test_twenty_turns_open_traces_through_one_client(fake_sdk):
    for turn in range(20):
        trace, callbacks = runtime_stream._open_trace("chat-turn-stream", f"thread-{turn}", "hello")
        assert trace is not None and len(callbacks) == 2

    assert _FakeLangfuse.constructed == 1, "a client per turn is a thread leak"


async def test_a_streamed_turn_with_a_trace_constructs_no_client_of_its_own(fake_sdk):
    """The old `finally` built a fresh client just to flush it."""

    class _EmptyGraph:
        async def astream_events(self, graph_input, config=None, version="v2"):
            return
            yield  # pragma: no cover

        async def aget_state(self, cfg):
            from types import SimpleNamespace

            from langchain_core.messages import AIMessage

            return SimpleNamespace(next=(), values={"messages": [AIMessage(content="hi")], "iterations": 1})

    constructed_before = _FakeLangfuse.constructed
    async for _ in runtime_stream._run_graph_stream(_EmptyGraph(), {}, {"configurable": {}}, trace=_FakeTrace()):
        pass

    assert _FakeLangfuse.constructed == constructed_before


def test_tracing_is_optional_a_construction_failure_degrades_to_no_trace(monkeypatch):
    import langfuse

    attempts = []

    def broken(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("langfuse unreachable at startup")

    monkeypatch.setattr(langfuse, "Langfuse", broken)
    monkeypatch.setattr(runtime_stream, "CallbackHandler", _FakeCallbackHandler)

    for _ in range(5):
        trace, callbacks = runtime_stream._open_trace("chat-turn-stream", "thread", "hello")
        assert trace is None
        assert len(callbacks) == 1, "the metrics handler still rides along"

    assert len(attempts) == 1, "a failed construction must not be retried on every turn"


def test_shutdown_flushes_and_stops_the_client_once(fake_sdk):
    client = tracing.get_langfuse()

    tracing.shutdown_langfuse()
    tracing.shutdown_langfuse()

    assert client.flushes == 1 and client.shutdowns == 1


def test_shutdown_without_a_client_is_a_no_op():
    tracing.shutdown_langfuse()  # must not raise


def test_a_client_is_not_resurrected_after_shutdown(fake_sdk):
    tracing.get_langfuse()
    tracing.shutdown_langfuse()

    assert tracing.get_langfuse() is None and _FakeLangfuse.constructed == 1


def test_a_failing_shutdown_never_raises(fake_sdk):
    client = tracing.get_langfuse()
    client.shutdown = lambda: (_ for _ in ()).throw(RuntimeError("queue stuck"))

    tracing.shutdown_langfuse()  # must not raise


def test_twenty_real_turns_add_one_clients_threads_not_sixty():
    """Against the REAL SDK (keys unset, so nothing is ever sent): the old code
    added three threads per `_open_trace` call."""
    before = threading.active_count()

    for turn in range(20):
        runtime_stream._open_trace("chat-turn-stream", f"thread-{turn}", "hello")

    added = threading.active_count() - before
    assert added <= 3, f"{added} threads added by 20 traces; one shared client adds three"


def test_a_flush_that_never_returns_does_not_hold_up_process_exit(monkeypatch, caplog):
    """`flush()` joins the client's queue and the queue retries a batch it cannot deliver, so with Langfuse unreachable it
    measured ~90 s at exit. Exit waits for it a bounded time, then gives up and says so."""
    release = threading.Event()

    class _StuckFlush(_FakeLangfuse):
        def flush(self):
            release.wait(30)  # a queue that cannot drain

    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", _StuckFlush)
    assert tracing.get_langfuse() is not None

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.core.tracing"):
        tracing.shutdown_langfuse(timeout_seconds=0.2)
    waited = time.monotonic() - started
    release.set()

    assert waited < 3, f"exit waited {waited:.1f}s for a flush that was never going to finish"
    assert [r.getMessage() for r in caplog.records] == ["tracing_client_shutdown_timed_out"]
    assert caplog.records[0].timeout_seconds == 0.2


def test_a_flush_that_finishes_in_time_is_followed_by_the_shutdown(fake_sdk):
    client = tracing.get_langfuse()

    tracing.shutdown_langfuse(timeout_seconds=5)

    assert (client.flushes, client.shutdowns) == (1, 1)

