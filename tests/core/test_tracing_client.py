"""One Langfuse client per process, not one per turn.

`_open_trace` built `Langfuse()` for every turn and `_run_graph_stream`'s
`finally` built ANOTHER just to call `.flush()` on it. Each construction starts
three background threads that nothing ever stops — measured against the installed
SDK (2.60.10), keys set or not: +3 threads per client, so a worker gained six
threads per turn (60 after ten) and never gave one back (spec 008, B18).

The flush was also wrong, not just wasteful: `Langfuse.flush()` joins THAT
instance's own ingestion queue, so flushing a brand-new client flushed an empty
queue and never the trace's events — the comment's promise ("so the trace is sent
even if the caller exits immediately") had never been kept. A correct flush
blocks until the queue drains, so it cannot be called on the event loop either:
events are sent by the SDK's own background consumer as they arrive, and the
shared client is flushed and shut down once, at process exit.

The leak itself is checked against the real SDK, since a counting fake cannot show
threads; behaviour around it is checked with a fake.
"""
import threading

import pytest

from app.agent import runtime_stream
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
