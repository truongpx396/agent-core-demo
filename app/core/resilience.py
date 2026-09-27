"""Retry-with-backoff + circuit-breaker for calls into this app's shared
LOCAL external dependencies — opensandbox-server
(app/domains/sandbox_tools.py) and crawl4ai (app/ingestion/web_crawler.py).
Both are docker-compose services that can be genuinely, briefly unreachable
(still starting, momentarily restarted) independent of anything this app's
own code did wrong — the exact "opensandbox-mcp is unreachable" scenario
app/domains/sandbox_session.py's own self-healing-cache docstring already
describes for the "already warmed up" side; this module is the missing
"still cold" half.

Deliberately narrow, matching this app's existing "surface the error, don't
blindly retry" posture (see app/ingestion/ingest_worker.py::process_job's own
docstring: most tool failures are deterministic, so retrying them just
delays the same failure). `CircuitBreaker.call` only ever retries exceptions
the CALLER explicitly names via `retry_on` — never a bare `except
Exception` — so a caller must justify, case by case, that a given exception
type means "this call never actually landed" (a refused/timed-out
connection) rather than "it landed, and the answer is a real failure" (bad
args, a 4xx, an application-level error already inside a response).
Retrying the latter would just burn time reproducing the same failure, or
worse, risk repeating a side effect that already happened — which is also
exactly why nothing in app/domains/sandbox_session.py's own raw MCP tool
calls (command_run, file_write, ...) is wrapped here: a connection dropping
AFTER a command was already dispatched into a sandbox is indistinguishable,
from out here, from one dropping before, and retrying could re-run a
non-idempotent command a second time. Both dependencies wrapped in this
module ARE read-only or safely re-attempted from scratch (listing a tool
catalog, rendering a page) with nothing to duplicate.

Circuit-breaking is the other half: once `failure_threshold` consecutive
`.call()`s have exhausted their own retries, `.call()` starts raising
`CircuitOpenError` immediately, without even attempting the network — for
`cooldown_seconds` — then lets exactly the next call through as a trial
(its own outcome decides whether the breaker re-opens or fully closes).
Without this, every single call made while a dependency is actually down
still pays its full connect-timeout, every time, for every concurrent
caller — worse than not retrying at all under real, simultaneous,
per-tenant traffic.

Per-process, in-memory state (a plain asyncio.Lock, not Redis) — good
enough here since both dependencies backed by this are local docker-compose
services, not something this app's horizontally-scaled workers need a
consistent shared view of (GRAPH_PATTERNS.md pattern 43). One replica's
breaker opening slightly ahead of another's is an acceptable inefficiency,
not a correctness bug — same "narrow accepted race, not worth closing"
posture as app/agent/tool_idempotency.py's own docstring.
"""
import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core import metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised by `CircuitBreaker.call` instead of even attempting the call,
    while the breaker is open. Callers generally want to treat this exactly
    like the dependency's own "unreachable" exception — it's the same
    outcome, just detected without paying for another failed attempt."""


class CircuitBreaker:
    """One dependency's rolling failure memory. Not a tool itself — wrap a
    call site's own zero-arg async function with `.call(fn, retry_on=...)`.

    `failure_threshold` consecutive `.call()`s that each exhaust their own
    retries opens the breaker; it then rejects every call for
    `cooldown_seconds` before admitting exactly one trial. Any success
    (first try or after a retry) fully resets it, whether or not the
    breaker was open — a real recovery doesn't need to wait out the rest of
    a cooldown it triggered before proving itself healthy again.
    """

    def __init__(self, name: str, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        retry_on: tuple[type[BaseException], ...],
        attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 4.0,
    ) -> T:
        """Runs `fn()`, retrying up to `attempts` total tries on any
        exception in `retry_on` — exponential backoff from `base_delay`,
        doubling each attempt and capped at `max_delay`, plus up to 25%
        random jitter so a burst of callers hitting the same outage don't
        all retry in lockstep. Any other exception `fn` raises propagates
        immediately, on the first attempt, untouched — never retried, and
        never counted against this breaker (it isn't evidence the
        dependency itself is unreachable).

        Fails fast with `CircuitOpenError` — without calling `fn` at all —
        if this dependency has already failed `failure_threshold`+ times in
        a row within the last `cooldown_seconds`.
        """
        await self._before_call()
        try:
            result = await self._retry(
                fn, retry_on=retry_on, attempts=attempts, base_delay=base_delay, max_delay=max_delay
            )
        except retry_on:
            await self._record_failure()
            raise
        await self._record_success()
        return result

    async def _retry(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        retry_on: tuple[type[BaseException], ...],
        attempts: int,
        base_delay: float,
        max_delay: float,
    ) -> T:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        for attempt in range(1, attempts + 1):
            try:
                return await fn()
            except retry_on as exc:
                if attempt == attempts:
                    raise
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay *= 1 + random.uniform(0, 0.25)
                metrics.agent_tool_retry_total.labels(dependency=self.name).inc()
                logger.info(
                    "resilience_retry",
                    extra={
                        "dependency": self.name,
                        "attempt": attempt,
                        "error_class": type(exc).__name__,
                        "delay_seconds": round(delay, 2),
                    },
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: loop above always returns or raises")

    async def _before_call(self) -> None:
        async with self._lock:
            opened_at = self._opened_at
        if opened_at is None:
            return
        remaining = self.cooldown_seconds - (time.monotonic() - opened_at)
        if remaining > 0:
            metrics.agent_circuit_breaker_rejected_total.labels(dependency=self.name).inc()
            raise CircuitOpenError(
                f"{self.name} looks down (failed {self.failure_threshold}+ times in a row) — "
                f"not retrying for another {remaining:.0f}s so as not to pile onto it."
            )

    async def _record_success(self) -> None:
        async with self._lock:
            was_open = self._opened_at is not None
            self._consecutive_failures = 0
            self._opened_at = None
        if was_open:
            logger.info("circuit_breaker_closed", extra={"dependency": self.name})

    async def _record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            newly_open = self._consecutive_failures >= self.failure_threshold
            if newly_open:
                self._opened_at = time.monotonic()
        if newly_open:
            metrics.agent_circuit_breaker_opened_total.labels(dependency=self.name).inc()
            logger.warning(
                "circuit_breaker_opened",
                extra={"dependency": self.name, "consecutive_failures": self._consecutive_failures},
            )
