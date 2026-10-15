"""The tenant allowance on the paths that are NOT a brand-new turn (spec 008 A6, FR-022).

Only `astream_events_turn` used to check the allowance and hold a reservation. A
turn paused for approval is resumed later through `astream_events_resume` — after
a wait of any length, during which the tenant may have spent its way past its
ceiling — and that path called the model again with no check and no hold. These pin
the rule per entry point:

  * resume: refused when the tenant is over its ceiling (the thread stays paused;
    cancelling it is never refused), and it holds a reservation while it runs;
  * the unattended decline chain: part of an already-admitted request, so it is not
    re-checked (a refusal there would strand the conversation at its pause);
  * crash-continue: a retry of admitted work, so it is NOT refused (that could strand
    a turn that already ran a mutating tool) but it does hold a reservation, since the
    crashed worker's was lost with it;
  * cancel: spends nothing, so it is never gated.

Fake graphs and patched budget functions: this proves each entry point asks the
right question and releases what it took, not how a real checkpointer behaves.
"""
import asyncio

import pytest

from app.agent import budgets
from app.agent import runtime as runtime_module
from app.agent import runtime_stream as stream_module
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value


class _GraphTouched(AssertionError):
    pass


@pytest.fixture
def budget(monkeypatch):
    """Records the allowance checks, reservations and releases of one test; the
    tenant is under budget unless the test flips `over`."""
    log = {"checks": 0, "reserved": [], "released": [], "over": False}

    async def refusal(ctx):
        log["checks"] += 1
        return budgets.refusal_envelope(budgets.Allowance("exceeded")) if log["over"] else None

    async def reserve(ctx):
        log["reserved"].append(ctx)
        return "hold-1"

    async def release(ctx, hold_id):
        log["released"].append(hold_id)

    monkeypatch.setattr(runtime_module, "_allowance_refusal", refusal)
    monkeypatch.setattr(runtime_module, "_reserve_turn_budget", reserve)
    monkeypatch.setattr(runtime_module, "_release_turn_budget", release)
    return log


def _graph_fails(monkeypatch):
    async def boom(*args, **kwargs):
        raise _GraphTouched("the graph must not be touched")

    monkeypatch.setattr(runtime_module, "init_graph_async", boom)


async def _drain_release_tasks():
    """The release is fire-and-forget by design; let the detached task finish."""
    for _ in range(3):
        await asyncio.sleep(0)


async def _collect(agen):
    return [event async for event in agen]


class TestResume:
    async def test_an_over_budget_tenant_is_refused_before_any_graph_work(self, monkeypatch, budget):
        budget["over"] = True
        _graph_fails(monkeypatch)
        rejected = metric_value(metrics.agent_requests_total, outcome="rejected")

        events = await _collect(stream_module.astream_events_resume("t1", True, TEST_CTX))

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == errors.ErrorCode.TENANT_BUDGET_EXCEEDED.value
        assert metric_value(metrics.agent_requests_total, outcome="rejected") == rejected + 1
        assert budget["reserved"] == []  # a refused resume takes no hold

    async def test_an_over_budget_tenant_cannot_resume_even_with_a_rejection(self, monkeypatch, budget):
        """A decline also lets the model react with another call, so it is spend too."""
        budget["over"] = True
        _graph_fails(monkeypatch)

        events = await _collect(stream_module.astream_events_resume("t1", False, TEST_CTX))

        assert events[0]["code"] == errors.ErrorCode.TENANT_BUDGET_EXCEEDED.value

    async def test_an_under_budget_tenant_reaches_the_graph_holding_a_reservation(self, monkeypatch, budget):
        _graph_fails(monkeypatch)

        with pytest.raises(_GraphTouched):
            await _collect(stream_module.astream_events_resume("t1", True, TEST_CTX))
        await _drain_release_tasks()

        assert budget["checks"] == 1
        assert budget["reserved"] == [TEST_CTX]
        assert budget["released"] == ["hold-1"]  # released even though the run blew up

    async def test_an_admitted_resume_skips_the_check_but_still_holds_a_reservation(self, monkeypatch, budget):
        budget["over"] = True  # would be refused if it were checked
        _graph_fails(monkeypatch)

        with pytest.raises(_GraphTouched):
            await _collect(stream_module.astream_events_resume("t1", False, TEST_CTX, admitted=True))
        await _drain_release_tasks()

        assert budget["checks"] == 0
        assert budget["released"] == ["hold-1"]

    async def test_the_hold_is_released_when_the_caller_stops_reading_early(self, monkeypatch, budget):
        """A client that disconnects closes the generator mid-stream; the hold must
        not outlive it (it would otherwise count as in-flight until it goes stale)."""

        class _Graph:
            async def aget_state(self, cfg):
                raise AssertionError("not used")

        async def init():
            return _Graph()

        async def no_error(graph, cfg):
            return None

        async def stream(graph, graph_input, cfg, trace, cancel_check=None):
            yield {"type": "token", "content": "a"}
            yield {"type": "token", "content": "b"}

        monkeypatch.setattr(runtime_module, "init_graph_async", init)
        monkeypatch.setattr(stream_module, "resumability_error_async", no_error)
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a: (None, []))
        monkeypatch.setattr(stream_module, "_run_graph_stream", stream)

        agen = stream_module.astream_events_resume("t1", True, TEST_CTX)
        assert (await agen.__anext__())["content"] == "a"
        await agen.aclose()
        await _drain_release_tasks()

        assert budget["released"] == ["hold-1"]


class TestUnattendedDeclineChain:
    async def test_every_decline_round_is_passed_as_already_admitted(self, monkeypatch):
        seen = []

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "approval_required", "tool_calls": [{"name": "add_note"}]}

        async def fake_resume(thread_id, approved, ctx, *, admitted=False):
            seen.append((approved, admitted))
            yield {"type": "done"}

        monkeypatch.setattr(stream_module, "astream_events_turn", fake_turn)
        monkeypatch.setattr(stream_module, "astream_events_resume", fake_resume)

        await _collect(stream_module.astream_events_turn_unattended("q", "t1", TEST_CTX))

        assert seen == [(False, True)]


class TestContinueTurn:
    async def test_a_crash_continue_is_not_refused_but_holds_a_reservation(self, monkeypatch, budget):
        budget["over"] = True  # an over-budget tenant's admitted turn still finishes
        _graph_fails(monkeypatch)

        with pytest.raises(_GraphTouched):
            await _collect(stream_module.astream_events_continue_turn("t1", TEST_CTX))
        await _drain_release_tasks()

        assert budget["checks"] == 0
        assert budget["reserved"] == [TEST_CTX]
        assert budget["released"] == ["hold-1"]


class TestCancel:
    async def test_cancelling_a_paused_run_is_never_gated_by_the_allowance(self, monkeypatch, budget):
        budget["over"] = True
        calls = []

        class _Graph:
            async def ainvoke(self, command, config=None):
                calls.append(command)

        async def init():
            return _Graph()

        async def no_error(graph, cfg):
            return None

        monkeypatch.setattr(runtime_module, "init_graph_async", init)
        monkeypatch.setattr(stream_module, "resumability_error_async", no_error)

        assert await stream_module.cancel_run("t1", TEST_CTX) is True

        assert len(calls) == 1
        assert budget["checks"] == 0
        assert budget["reserved"] == []
