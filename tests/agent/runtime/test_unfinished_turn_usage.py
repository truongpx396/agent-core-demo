"""A turn that does not finish still spent tokens, and the tokens metric must say so.

`_run_graph_stream` counted tokens only on its success branch; the timeout, error and cancel branches called
`_record_turn_metrics` with no state, assuming "total_tokens == 0 anyway". That's false: the model had
answered some steps and the checkpoint holds their cost (spec 008, B17; reproduced with a model returning a
500-token step and then stalling past the timeout). So `_record_unfinished_turn` reads the last checkpoint,
bounded, and feeds `agent_tokens_total`.

It also fed the per-turn `usage_ledger` row, which made this matter for MONEY; that is over (specs/010
T030c2): the dollar caps sum usage events written when each call returns
(tests/agent/cost/test_usage_events.py). These tests pin the metric, and that reading the checkpoint can never
make a failing turn worse.

The fake graph is the minimum `_run_graph_stream` needs: an event stream ending in the chosen failure and a
checkpoint already holding the spend. That proves the function reads the checkpoint on each unfinished path
and counts what it finds, not what a real checkpointer holds when a model call is cut off mid-flight (tokens
of an in-flight call never reach a checkpoint and aren't counted).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agent.runtime import stream as stream_module
from app.core import metrics
from tests.conftest import TEST_CTX, metric_value

THREAD = "thread-unfinished"


class _GraphThatSpentThenFailed:
    """`astream_events` yields nothing and then fails with `exc`; `aget_state`
    answers like a checkpointer that already holds `values`."""

    def __init__(self, exc, values=None, *, state_error=None, state_delay=0.0):
        self._exc = exc
        self._values = (
            values
            if values is not None
            else {"total_tokens": 500, "total_cost_usd": 0.0125, "iterations": 2}
        )
        self._state_error = state_error
        self._state_delay = state_delay
        self.state_reads = 0

    async def astream_events(self, graph_input, config=None, version="v2"):
        if self._exc is not None:
            raise self._exc
        return
        yield  # pragma: no cover - makes this an async generator

    async def aget_state(self, cfg):
        self.state_reads += 1
        if self._state_delay:
            await asyncio.sleep(self._state_delay)
        if self._state_error is not None:
            raise self._state_error
        return SimpleNamespace(values=self._values, next=(), tasks=[])


@pytest.fixture
def iterations_observed(monkeypatch):
    seen: list = []
    monkeypatch.setattr(metrics.agent_iterations, "observe", lambda value: seen.append(value))
    return seen


def _cfg(ctx=TEST_CTX):
    return {"configurable": {"thread_id": THREAD, "ctx": ctx}}


async def _run(graph, *, cancel_check=None, cfg=None):
    return [
        event
        async for event in stream_module._run_graph_stream(
            graph, {}, cfg or _cfg(), trace=None, cancel_check=cancel_check
        )
    ]


async def _always_cancelled():
    return True


# --- every unfinished outcome counts what was spent -------------------------------


def _tokens_counted(before: float) -> float:
    return metric_value(metrics.agent_tokens_total) - before


async def test_a_timed_out_turn_counts_the_tokens_it_already_spent():
    tokens_before = metric_value(metrics.agent_tokens_total)

    events = await _run(_GraphThatSpentThenFailed(TimeoutError()))

    assert [e["type"] for e in events] == ["error"] and events[0]["code"] == "timeout"
    assert _tokens_counted(tokens_before) == 500


async def test_a_turn_that_errored_counts_the_tokens_it_already_spent():
    tokens_before = metric_value(metrics.agent_tokens_total)

    events = await _run(_GraphThatSpentThenFailed(RuntimeError("boom")))

    assert events[0]["code"] == "internal"
    assert _tokens_counted(tokens_before) == 500


async def test_a_turn_the_user_cancelled_counts_the_tokens_it_already_spent():
    tokens_before = metric_value(metrics.agent_tokens_total)

    events = await _run(_GraphThatSpentThenFailed(None), cancel_check=_always_cancelled)

    assert events[0]["code"] == "cancelled"
    assert _tokens_counted(tokens_before) == 500


async def test_a_task_cancellation_counts_the_tokens_and_still_propagates():
    """Real asyncio cancellation (a client disconnect, shutdown): the tokens are counted on the way out and the
    CancelledError is not swallowed."""
    tokens_before = metric_value(metrics.agent_tokens_total)

    with pytest.raises(asyncio.CancelledError):
        await _run(_GraphThatSpentThenFailed(asyncio.CancelledError()))

    assert _tokens_counted(tokens_before) == 500


# --- what must not change ---------------------------------------------------------


@pytest.mark.parametrize("exc", [TimeoutError(), RuntimeError("boom")])
async def test_an_unfinished_turn_does_not_feed_the_completed_turn_iterations_histogram(exc, iterations_observed):
    """`agent_iterations` is "how many round trips a TURN takes" on the overview
    dashboard; its meaning stays completed turns only."""
    await _run(_GraphThatSpentThenFailed(exc))

    assert iterations_observed == []


async def test_a_turn_that_spent_nothing_adds_nothing_to_the_tokens_metric():
    tokens_before = metric_value(metrics.agent_tokens_total)

    await _run(_GraphThatSpentThenFailed(TimeoutError(), values={"total_tokens": 0, "iterations": 0}))

    assert _tokens_counted(tokens_before) == 0


async def test_the_tokens_metric_needs_no_identity():
    """The money needs a tenant (it is a usage event, written where the call is made); a counter does not. A turn
    with no identity in its config still counts its tokens."""
    tokens_before = metric_value(metrics.agent_tokens_total)

    await _run(_GraphThatSpentThenFailed(TimeoutError()), cfg={"configurable": {"thread_id": THREAD}})

    assert _tokens_counted(tokens_before) == 500


async def test_the_outcome_is_still_counted_for_an_unfinished_turn():
    before = metric_value(metrics.agent_requests_total, outcome="timeout")

    await _run(_GraphThatSpentThenFailed(TimeoutError()))

    assert metric_value(metrics.agent_requests_total, outcome="timeout") - before == 1


# --- reading the checkpoint must never make a failure worse ------------------------


async def test_an_unreadable_checkpoint_still_ends_the_turn_with_its_terminal_event():
    tokens_before = metric_value(metrics.agent_tokens_total)
    graph = _GraphThatSpentThenFailed(TimeoutError(), state_error=ConnectionError("checkpointer down"))

    events = await _run(graph)

    assert graph.state_reads == 1
    assert [e["type"] for e in events] == ["error"] and events[0]["code"] == "timeout"
    assert _tokens_counted(tokens_before) == 0


async def test_a_checkpoint_read_that_hangs_is_abandoned_not_waited_for(monkeypatch):
    """A turn is ending BECAUSE something was slow; accounting must not add an
    unbounded wait to that."""
    monkeypatch.setattr(stream_module, "UNFINISHED_TURN_STATE_READ_TIMEOUT_SECONDS", 0.05)
    graph = _GraphThatSpentThenFailed(TimeoutError(), state_delay=30)

    events = await asyncio.wait_for(_run(graph), timeout=5)

    assert events[0]["code"] == "timeout"


async def test_a_failure_while_counting_does_not_change_the_terminal_event(monkeypatch):
    def broken(value):
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(metrics.agent_tokens_total, "add", broken, raising=False)
    monkeypatch.setattr(metrics.agent_tokens_total, "inc", broken, raising=False)

    events = await _run(_GraphThatSpentThenFailed(TimeoutError()))

    assert events[0]["code"] == "timeout"
