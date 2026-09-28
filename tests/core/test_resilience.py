"""Tests for app/core/resilience.py's CircuitBreaker: the retry-with-backoff
+ fail-fast primitive behind app/domains/sandbox_tools.py::load_sandbox_tools
and app/ingestion/web_crawler.py::_crawl (see that module's own docstring
for why it exists and what it deliberately does NOT cover).

Every test builds its own fresh CircuitBreaker instance — this state is
per-instance (an asyncio.Lock plus two plain attributes), so nothing here
needs the module-level singletons the two real call sites use, and no test
can leak failure/open state into another.

`base_delay`/`max_delay` are kept tiny (milliseconds) throughout so this
file's real `asyncio.sleep` calls don't slow the suite down; the one test
that cares about the actual delay VALUES chosen (growth + cap) monkeypatches
`asyncio.sleep` to record calls instead of sleeping.
"""
import asyncio

import pytest

from app.core import metrics
from app.core.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from tests.conftest import metric_value as _count


class _Boom(Exception):
    pass


class _OtherError(Exception):
    pass


def _calls(*, fail_times: int = 0, exc=_Boom, result: str = "ok"):
    """A zero-arg async callable that raises `exc` for its first
    `fail_times` invocations, then returns `result` — plus the mutable
    counter it was called with, so a test can assert exactly how many times
    it actually ran."""
    state = {"count": 0}

    async def fn():
        state["count"] += 1
        if state["count"] <= fail_times:
            raise exc(f"attempt {state['count']}")
        return result

    return fn, state


class TestCallSuccessAndRetry:
    async def test_succeeds_without_retry_on_the_first_try(self):
        breaker = CircuitBreaker("dep-a")
        fn, state = _calls(fail_times=0)

        result = await breaker.call(fn, retry_on=(_Boom,), attempts=3, base_delay=0.001)

        assert result == "ok"
        assert state["count"] == 1

    async def test_retries_a_retry_on_exception_and_eventually_succeeds(self):
        breaker = CircuitBreaker("dep-b")
        fn, state = _calls(fail_times=1)
        before = _count(metrics.agent_tool_retry_total, dependency="dep-b")

        result = await breaker.call(fn, retry_on=(_Boom,), attempts=3, base_delay=0.001)

        assert result == "ok"
        assert state["count"] == 2  # one failure, one successful retry
        assert _count(metrics.agent_tool_retry_total, dependency="dep-b") == before + 1

    async def test_exhausts_attempts_and_raises_the_last_exception(self):
        breaker = CircuitBreaker("dep-c")
        fn, state = _calls(fail_times=99)  # always fails

        with pytest.raises(_Boom):
            await breaker.call(fn, retry_on=(_Boom,), attempts=3, base_delay=0.001)

        assert state["count"] == 3  # every attempt used, no more

    async def test_a_non_retryable_exception_propagates_immediately(self):
        breaker = CircuitBreaker("dep-d")
        fn, state = _calls(fail_times=99, exc=_OtherError)

        with pytest.raises(_OtherError):
            await breaker.call(fn, retry_on=(_Boom,), attempts=3, base_delay=0.001)

        assert state["count"] == 1  # never retried

    async def test_a_non_retryable_failure_does_not_count_against_the_breaker(self):
        """_OtherError isn't evidence the DEPENDENCY is down — a caller that
        keeps raising it should never trip the breaker."""
        breaker = CircuitBreaker("dep-e", failure_threshold=2, cooldown_seconds=60)
        fn, _ = _calls(fail_times=99, exc=_OtherError)

        for _ in range(5):
            with pytest.raises(_OtherError):
                await breaker.call(fn, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        # Still closed: a call that would succeed goes through untouched.
        ok_fn, _ = _calls(fail_times=0)
        assert await breaker.call(ok_fn, retry_on=(_Boom,), attempts=1, base_delay=0.001) == "ok"

    async def test_delay_grows_and_is_capped_with_jitter(self, monkeypatch):
        breaker = CircuitBreaker("dep-f")
        fn, _ = _calls(fail_times=4, exc=_Boom)
        recorded_delays = []

        async def fake_sleep(seconds):
            recorded_delays.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        monkeypatch.setattr("random.uniform", lambda a, b: 0)  # isolate growth from jitter

        await breaker.call(fn, retry_on=(_Boom,), attempts=5, base_delay=1.0, max_delay=3.0)

        # base_delay * 2**0, 2**1, 2**2, then capped at max_delay
        assert recorded_delays == [1.0, 2.0, 3.0, 3.0]


class TestCircuitBreakerOpening:
    async def test_opens_after_failure_threshold_consecutive_exhausted_calls(self):
        breaker = CircuitBreaker("dep-g", failure_threshold=2, cooldown_seconds=60)
        always_fails, state = _calls(fail_times=99)
        before_opened = _count(metrics.agent_circuit_breaker_opened_total, dependency="dep-g")

        for _ in range(2):
            with pytest.raises(_Boom):
                await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        assert _count(metrics.agent_circuit_breaker_opened_total, dependency="dep-g") == before_opened + 1

        # Breaker now open: a THIRD call must fail fast, never touching fn.
        calls_before = state["count"]
        before_rejected = _count(metrics.agent_circuit_breaker_rejected_total, dependency="dep-g")
        with pytest.raises(CircuitOpenError):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        assert state["count"] == calls_before  # fn was never invoked
        assert _count(metrics.agent_circuit_breaker_rejected_total, dependency="dep-g") == before_rejected + 1

    async def test_a_success_never_counts_toward_opening_it(self):
        breaker = CircuitBreaker("dep-h", failure_threshold=2, cooldown_seconds=60)
        succeed, _ = _calls(fail_times=0)

        # Alternating fail-then-recover, fail-then-recover: never TWO
        # consecutive exhausted failures, so it should never open.
        for _ in range(4):
            fn, _ = _calls(fail_times=1)
            await breaker.call(fn, retry_on=(_Boom,), attempts=2, base_delay=0.001)

        result = await breaker.call(succeed, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert result == "ok"

    async def test_cooldown_elapsing_admits_a_trial_that_fully_closes_on_success(self):
        breaker = CircuitBreaker("dep-i", failure_threshold=1, cooldown_seconds=0.05)
        always_fails, _ = _calls(fail_times=99)

        with pytest.raises(_Boom):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        # Still within cooldown: fails fast.
        with pytest.raises(CircuitOpenError):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        await asyncio.sleep(0.06)  # let the cooldown elapse

        succeed, state = _calls(fail_times=0)
        result = await breaker.call(succeed, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert result == "ok"
        assert state["count"] == 1

        # Fully closed again: takes a fresh failure_threshold (1) failure
        # to reopen, not a leftover count from before the trial succeeded.
        reopens, reopen_state = _calls(fail_times=99)
        with pytest.raises(_Boom):
            await breaker.call(reopens, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert reopen_state["count"] == 1

    async def test_cooldown_elapsing_with_a_failing_trial_reopens_it(self):
        breaker = CircuitBreaker("dep-j", failure_threshold=1, cooldown_seconds=0.05)
        always_fails, state = _calls(fail_times=99)

        with pytest.raises(_Boom):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        await asyncio.sleep(0.06)

        # Trial call is admitted (fn IS invoked) but fails again.
        calls_before = state["count"]
        with pytest.raises(_Boom):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert state["count"] == calls_before + 1

        # Re-opened: the next call fails fast again without invoking fn.
        calls_before = state["count"]
        with pytest.raises(CircuitOpenError):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert state["count"] == calls_before


class TestCircuitStateTransitions:
    """`.state` (`CircuitState.CLOSED`/`OPEN`/`HALF_OPEN`) made explicit —
    same transitions `TestCircuitBreakerOpening` already covers by
    behavior, asserted here directly against the enum."""

    async def test_starts_closed(self):
        assert CircuitBreaker("dep-state-a").state is CircuitState.CLOSED

    async def test_moves_to_open_after_threshold_failures(self):
        breaker = CircuitBreaker("dep-state-b", failure_threshold=1, cooldown_seconds=60)
        always_fails, _ = _calls(fail_times=99)

        with pytest.raises(_Boom):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        assert breaker.state is CircuitState.OPEN

    async def test_moves_to_half_open_the_instant_cooldown_elapses(self):
        """Asserted from OUTSIDE `.call()` — via a slow trial `fn` held open
        with an event — so this observes the HALF_OPEN state itself, not
        just its eventual CLOSED/OPEN outcome (already covered above)."""
        breaker = CircuitBreaker("dep-state-c", failure_threshold=1, cooldown_seconds=0.02)
        always_fails, _ = _calls(fail_times=99)
        with pytest.raises(_Boom):
            await breaker.call(always_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        await asyncio.sleep(0.03)

        release_trial = asyncio.Event()

        async def slow_trial():
            return await release_trial.wait()

        trial_task = asyncio.create_task(
            breaker.call(slow_trial, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        )
        try:
            async def _wait_for_half_open():
                while breaker.state is not CircuitState.HALF_OPEN:
                    await asyncio.sleep(0)

            await asyncio.wait_for(_wait_for_half_open(), timeout=1)
            assert breaker.state is CircuitState.HALF_OPEN
        finally:
            release_trial.set()
            await trial_task


class TestHalfOpenSingleFlight:
    """The gap the plain-timestamp version of this breaker used to leave
    open: once cooldown elapses, EVERY concurrently-arriving caller used to
    see the same "cooldown has passed" snapshot and get admitted as its own
    trial — a burst hitting a dependency that's only just allowed to prove
    itself healthy again, not the single gentle probe a real half-open
    state is supposed to be. `_before_call` now flips CircuitState under
    `_lock` before releasing it, so only the FIRST caller to observe
    cooldown-elapsed becomes the trial; every other one is rejected the
    same as a still-open breaker.
    """

    async def test_a_second_caller_is_rejected_while_the_trial_is_in_flight(self):
        breaker = CircuitBreaker("dep-k", failure_threshold=1, cooldown_seconds=0.02)
        first_fails, _ = _calls(fail_times=99)
        with pytest.raises(_Boom):
            await breaker.call(first_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        await asyncio.sleep(0.03)  # cooldown elapses

        trial_started = asyncio.Event()
        release_trial = asyncio.Event()

        async def slow_trial():
            trial_started.set()
            await release_trial.wait()
            return "ok"

        trial_task = asyncio.create_task(
            breaker.call(slow_trial, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        )
        await trial_started.wait()  # the trial is now genuinely in flight
        assert breaker.state is CircuitState.HALF_OPEN

        # A second caller arriving while that trial is still unresolved
        # must be rejected outright — NOT admitted as a second, concurrent
        # trial — and must never invoke its own fn.
        second_fn, second_state = _calls(fail_times=0)
        before_rejected = _count(metrics.agent_circuit_breaker_rejected_total, dependency="dep-k")
        with pytest.raises(CircuitOpenError):
            await breaker.call(second_fn, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        assert second_state["count"] == 0
        assert _count(metrics.agent_circuit_breaker_rejected_total, dependency="dep-k") == before_rejected + 1

        release_trial.set()
        assert await trial_task == "ok"
        assert breaker.state is CircuitState.CLOSED  # the (only) trial succeeded

    async def test_exactly_one_half_open_metric_per_cooldown_even_with_concurrent_arrivals(self):
        breaker = CircuitBreaker("dep-l", failure_threshold=1, cooldown_seconds=0.02)
        first_fails, _ = _calls(fail_times=99)
        with pytest.raises(_Boom):
            await breaker.call(first_fails, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        await asyncio.sleep(0.03)
        before_half_open = _count(metrics.agent_circuit_breaker_half_open_total, dependency="dep-l")

        trial_started = asyncio.Event()
        release_trial = asyncio.Event()

        async def slow_trial():
            trial_started.set()
            return await release_trial.wait()

        trial_task = asyncio.create_task(
            breaker.call(slow_trial, retry_on=(_Boom,), attempts=1, base_delay=0.001)
        )
        await trial_started.wait()  # don't race the "losers" below against _before_call itself

        # Several more callers race in while the trial is still pending —
        # none of them should ALSO be counted as admitted half-open trials.
        losers, _ = _calls(fail_times=99)
        for _ in range(3):
            with pytest.raises(CircuitOpenError):
                await breaker.call(losers, retry_on=(_Boom,), attempts=1, base_delay=0.001)

        release_trial.set()
        await trial_task

        assert _count(metrics.agent_circuit_breaker_half_open_total, dependency="dep-l") == before_half_open + 1
