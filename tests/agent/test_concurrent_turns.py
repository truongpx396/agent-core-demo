"""Dedicated concurrency-correctness suite — does running several turns at
once (agent_worker.py's own `_MAX_CONCURRENCY` + `app/agent/runtime.py`'s
pooled checkpointer, both added specifically to let ONE worker process run
multiple turns simultaneously instead of one at a time) actually produce
correct, isolated results, or does it corrupt/cross-deliver state between
concurrent turns?

Separate from tests/agent/test_durable_checkpoint.py (this file's closest
sibling, same "real Postgres, fake LLM, no live Ollama" shape, same
`ensure_postgres()`/`reset_agent_singleton` pattern) because that file's own
concurrent-access tests (`TestResumabilityErrorRejectsAnActivelyRunningThread`)
are about a SPECIFIC race (a stale/racing resume hijacking an
actively-streaming turn) — this file is about the general question a reader
of that concurrency work would reasonably ask next: "does it work correctly
under real concurrent load, with no shared-data contention or cache leak
between unrelated turns?" Runs on CI (`make test-integration`,
`@pytest.mark.integration`) against real, ephemeral Postgres + Redis Stack
containers (tests/containers.py) and a fake LLM (langchain_core's
GenericFakeChatModel, same technique test_durable_checkpoint.py already
uses) — no live Ollama/LiteLLM dependency, so this runs in ordinary CI, not
just by hand against `make up`.

Three angles, one per test class below:
- TestNoCrossContaminationUnderConcurrency: N turns on N different THREADS,
  run truly concurrently against the real pooled checkpointer — does each
  thread's checkpointed history end up containing ONLY its own message?
- TestSemanticCacheIsolationUnderConcurrency: two different TENANTS racing
  an identical question against the real (not stubbed) semantic cache —
  does a same-tenant repeat correctly hit while a different tenant's first
  ask of the identical text, at the same real moment, correctly misses?
- TestWorkerConcurrencyAgainstTheRealQueue: the actual production dispatch
  code (`agent_worker._process_with_limit`, the same Semaphore-gated
  asyncio.create_task shape `run()` itself uses) against a real Redis
  queue — do N deliberately-slow turns actually finish in genuinely
  overlapping wall-clock time, or does something silently re-serialize
  them? This is the regression guard: if a future change reintroduces
  serial processing, this test's timing assertion catches it even though
  every individual turn would still complete "correctly."
"""
import asyncio
import itertools
import time
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import runtime as agent_module
from app.agent.graph import GraphDeps, build_graph
from app.core import metrics
from app.retrieval import semantic_cache
from app.turns import agent_worker, queue
from tests.conftest import metric_value as _count
from tests.containers import ensure_postgres, ensure_redis

pytestmark = pytest.mark.integration


def _ctx(tenant: str, principal: str = "test-user") -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


def _no_op_search(query: str, ctx) -> tuple[str, list[dict]]:
    """Stubs retrieval only — these tests are about concurrency isolation
    in the checkpointer/cache/queue, not retrieval quality, so this keeps
    the suite from needing a real Qdrant container too."""
    return "", []


def _fixed_llm(text: str = "A sufficiently long final answer.", delay: float = 0.0):
    """A GenericFakeChatModel answering EVERY call with the same `text`
    (itertools.repeat never exhausts) — these tests care whether turns
    interfere with EACH OTHER, not whether each gets a distinct scripted
    reply, so one fixed response is enough. `delay` (same technique as
    tests/agent/test_durable_checkpoint.py's own `_SlowFakeLLM`) awaits
    between streamed chunks so several concurrent turns' LLM calls
    genuinely overlap in wall-clock time instead of the fake model
    returning instantly — needed for the overlap-timing assertion in
    TestWorkerConcurrencyAgainstTheRealQueue below."""

    class _Model(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
                if delay:
                    await asyncio.sleep(delay)

    return _Model(messages=itertools.repeat(AIMessage(content=text)))


@pytest.fixture(autouse=True)
def reset_agent_singleton(monkeypatch):
    """Same fixture as tests/agent/test_durable_checkpoint.py — see that
    file's docstring for why `_graph`/`_checkpointer_pool` (module-level
    singletons) need resetting before and after every test here too.

    Deliberately does NOT also close the checkpointer pool here (a first
    attempt at that hung this file's own teardown — see
    `run_with_checkpointer_cleanup` below for why and the actual fix)."""
    monkeypatch.setattr(
        agent_module, "CHECKPOINTER_DATABASE_URL", ensure_postgres()["checkpointer_database_url"]
    )
    agent_module._graph = None
    agent_module._checkpointer_pool = None
    yield
    agent_module._graph = None
    agent_module._checkpointer_pool = None


def run_with_checkpointer_cleanup(coro_fn):
    """`asyncio.run(coro_fn())`, but closes the checkpointer pool INSIDE
    that same event loop, right before it returns — not afterward, e.g. in
    a fixture teardown's own separate `asyncio.run()` call. Verified
    directly that the separate-call version hangs/raises CancelledError:
    `AsyncConnectionPool` is loop-bound the same way this module's own
    docstring already documents for `AsyncPostgresSaver` itself (its
    background worker tasks live on whichever loop `pool.open()` ran on),
    so `pool.close()` from a DIFFERENT, later loop can't cleanly join
    them. Every test below runs its whole turn-driving coroutine through
    this instead of a bare `asyncio.run(...)` specifically so the pool
    this file's real-Postgres tests open gets closed on the one loop
    that's actually allowed to close it."""

    async def _wrapped():
        try:
            return await coro_fn()
        finally:
            await agent_module.close_checkpointer_pool()

    return asyncio.run(_wrapped())


@pytest.fixture(autouse=True)
def real_semantic_cache_redis(monkeypatch):
    """Points the REAL app/retrieval/semantic_cache.py module (not stubbed
    via GraphDeps.cache_get/cache_set — isolation under concurrency is
    exactly what TestSemanticCacheIsolationUnderConcurrency exists to
    check) at a real, ephemeral Redis Stack container. `_client`/
    `_index_ready` reset the same way tests/integration/test_queue_real_redis.py
    resets its own module-level lazy-singleton client.

    `flushall()` matters here specifically (that file's own fixture doesn't
    need it — it scopes REQUESTS_STREAM/CONSUMER_GROUP to a fresh uuid per
    test instead): `ensure_redis()`'s container is SHARED and PERSISTENT —
    reused across every test in this session, and verified directly to
    survive across separate process invocations entirely (a throwaway
    debug script's cache write was still readable in a completely fresh
    python run afterward). semantic_cache.py's index name/key prefix are
    fixed module constants, not overridable per-test the way the queue's
    stream/group names are, so a flush is the only way to guarantee this
    file's cache-isolation assertions start from a genuinely empty cache
    instead of silently depending on whatever an earlier test (or an
    earlier whole pytest run reusing the same cached container) left
    behind."""
    info = ensure_redis()
    monkeypatch.setattr(semantic_cache, "REDIS_URL", info["redis_url"])
    monkeypatch.setattr(semantic_cache, "_client", None)
    monkeypatch.setattr(semantic_cache, "_index_ready", False)
    semantic_cache._get_client().flushall()
    yield


def _install_fake_graph(monkeypatch, llm) -> None:
    """`cache_get`/`cache_set` are wired explicitly to the REAL
    app/retrieval/semantic_cache.py module functions — GraphDeps's own
    documented test-injection seam (see its docstring: "tests set
    individual fields to inject fakes instead of monkeypatching module
    globals"). Needed specifically because tests/conftest.py's session-wide
    `mock_semantic_cache` autouse fixture always overrides
    app/agent/graph.py's `_default_cache_get`/`_default_cache_set` to a
    hardcoded miss/no-op (correctly, for every OTHER test in this suite,
    which must never need a live Redis/embedding call) — build_graph()
    falls back to those exact (mocked) module names whenever GraphDeps
    doesn't set cache_get/cache_set itself. Without this override, every
    test in this file would silently exercise that global mock instead of
    the real cache — verified directly: that's exactly what produced this
    file's first failed run of
    TestSemanticCacheIsolationUnderConcurrency (every lookup came back a
    hardcoded miss, not a real Redis round trip)."""
    monkeypatch.setattr(
        agent_module,
        "build_graph",
        lambda checkpointer=None, manifest=None, domain=None: build_graph(
            GraphDeps(
                llm=llm,
                search_docs=_no_op_search,
                cache_get=semantic_cache.get,
                cache_set=semantic_cache.set,
            ),
            checkpointer=checkpointer,
        ),
    )


class TestNoCrossContaminationUnderConcurrency:
    """The pooled checkpointer (app/agent/runtime.py::_open_checkpointer)
    replaced a single shared AsyncConnection specifically so concurrent
    turns don't all funnel through one connection — this is the most
    direct possible check that the pool genuinely ISOLATES them rather
    than cross-delivering state: N turns on N different threads, run truly
    concurrently, each thread's checkpointed history must end up
    containing ONLY its own message."""

    def test_n_concurrent_turns_on_different_threads_keep_separate_checkpointed_state(
        self, monkeypatch
    ):
        n = 8
        _install_fake_graph(monkeypatch, _fixed_llm())

        async def _run():
            graph = await agent_module.init_graph_async()
            thread_ids = [str(uuid.uuid4()) for _ in range(n)]

            async def drive(i: int) -> None:
                async for _ in agent_module.astream_events_turn(
                    f"remember the number {i}", thread_ids[i], _ctx(f"tenant-{i}")
                ):
                    pass

            await asyncio.gather(*(drive(i) for i in range(n)))

            return [
                await graph.aget_state({"configurable": {"thread_id": tid}}) for tid in thread_ids
            ]

        states = run_with_checkpointer_cleanup(_run)

        for i, state in enumerate(states):
            human_texts = [m.content for m in state.values["messages"] if isinstance(m, HumanMessage)]
            assert human_texts == [f"remember the number {i}"], (
                f"thread {i}'s checkpointed state was contaminated by a concurrent "
                f"sibling turn: {human_texts}"
            )


class TestSemanticCacheIsolationUnderConcurrency:
    """app/retrieval/semantic_cache.py scopes every read/write to
    ctx['tenant']+ctx['principal'] as a SERVER-SIDE RediSearch TAG
    predicate, not a Python post-filter (see that module's own docstring).
    Proves that holds under real concurrent access, not just by reading
    the code: races a same-tenant repeat of an already-cached question
    (must HIT) against a different tenant's first-ever ask of the
    IDENTICAL question text (must MISS despite a matching entry already
    sitting in the index) — both queries land on the real RediSearch index
    at the same real moment, the only way to actually exercise the TAG
    filter under concurrency rather than trusting it in isolation."""

    def test_a_different_tenants_first_ask_never_hits_another_tenants_cached_entry(
        self, monkeypatch
    ):
        _install_fake_graph(monkeypatch, _fixed_llm())
        question = "What is a LangGraph checkpointer?"

        async def drive(tenant: str) -> None:
            async for _ in agent_module.astream_events_turn(question, str(uuid.uuid4()), _ctx(tenant)):
                pass

        async def _run():
            await agent_module.init_graph_async()

            # Seed tenant-a's cache entry for this exact question first —
            # deliberately sequential, so the entry genuinely exists before
            # the race below starts.
            await drive("tenant-a")

            hits_before = _count(metrics.agent_semantic_cache_total, outcome="hit")
            misses_before = _count(metrics.agent_semantic_cache_total, outcome="miss")

            await asyncio.gather(
                drive("tenant-a"),  # same tenant, same question again -> must HIT
                drive("tenant-b"),  # different tenant, same question, first time -> must MISS
            )
            return hits_before, misses_before

        hits_before, misses_before = run_with_checkpointer_cleanup(_run)

        assert _count(metrics.agent_semantic_cache_total, outcome="hit") == hits_before + 1, (
            "tenant-a's repeat query should have hit its own cached entry — if this "
            "doesn't hold, the isolation assertion below would pass for the wrong "
            "reason (a cache that never hits anything looks 'isolated' too)"
        )
        assert _count(metrics.agent_semantic_cache_total, outcome="miss") == misses_before + 1, (
            "tenant-b's identical question incorrectly hit tenant-a's cached entry — "
            "a cross-tenant cache leak"
        )


class TestWorkerConcurrencyAgainstTheRealQueue:
    """Regression guard for the actual production dispatch code
    (app/turns/agent_worker.py::run's Semaphore-gated asyncio.create_task
    loop, exercised here via the same `_process_with_limit` helper `run()`
    itself calls) against a real Redis queue. N turns, each deliberately
    slow (a real `await asyncio.sleep` per streamed chunk, not instant),
    dispatched the exact same way `run()` dispatches them — if a future
    change silently reintroduces one-turn-at-a-time processing, every
    individual turn would still complete correctly, so only a wall-clock
    timing assertion like this one would actually catch the regression."""

    @pytest.fixture(autouse=True)
    def real_redis_for_queue(self, monkeypatch):
        """Same isolation technique as
        tests/integration/test_queue_real_redis.py's own `real_redis`
        fixture, applied to BOTH `queue`'s and `agent_worker`'s own
        `REQUESTS_STREAM`/`CONSUMER_GROUP` bindings — `agent_worker.py`
        imports those via `from app.turns.queue import (...)`, a separate
        reference `queue`'s own rebinding doesn't reach (see
        tests/conftest.py's docstring on this exact gotcha), and
        `process_request`'s `finally: client.xack(...)` reads them off
        `agent_worker`'s own module globals, not a parameter."""
        info = ensure_redis()
        stream = f"agent:requests:test:{uuid.uuid4()}"
        group = f"agent-workers:test:{uuid.uuid4()}"
        monkeypatch.setattr(queue, "REDIS_URL", info["redis_url"])
        monkeypatch.setattr(queue, "_client", None)
        monkeypatch.setattr(queue, "REQUESTS_STREAM", stream)
        monkeypatch.setattr(queue, "CONSUMER_GROUP", group)
        monkeypatch.setattr(agent_worker, "REQUESTS_STREAM", stream)
        monkeypatch.setattr(agent_worker, "CONSUMER_GROUP", group)
        yield
        queue._client = None

    def test_n_queued_turns_run_concurrently_not_back_to_back(self, monkeypatch):
        n = 5
        per_chunk_delay = 0.2  # "one two three four five" -> 5 chunks -> ~1s if run alone
        _install_fake_graph(monkeypatch, _fixed_llm("one two three four five", delay=per_chunk_delay))

        async def _run():
            await agent_module.init_graph_async()
            client = queue.get_client()
            await queue.ensure_consumer_group(client)

            request_ids = [str(uuid.uuid4()) for _ in range(n)]
            for i, rid in enumerate(request_ids):
                await queue.publish_request(
                    client,
                    request_id=rid,
                    # Distinct text per turn — keeps this test's timing signal
                    # clean regardless of the real semantic cache also being
                    # live (see real_semantic_cache_redis): identical text
                    # across turns could let a later one cache-hit and skip
                    # the LLM entirely, which would only make this
                    # assertion's margin more generous, but distinct text
                    # removes the question rather than relying on that.
                    text=f"say something slowly, turn {i}",
                    thread_id=str(uuid.uuid4()),
                    ctx=_ctx("tenant-worker-overlap"),
                )

            response = await client.xreadgroup(
                agent_worker.CONSUMER_GROUP,
                "test-consumer",
                {agent_worker.REQUESTS_STREAM: ">"},
                count=n,
            )
            _, entries = response[0]
            assert len(entries) == n

            # The EXACT dispatch shape app/turns/agent_worker.py::run uses:
            # acquire the semaphore BEFORE creating each task (so a full
            # worker backpressures reads, not just processing), release
            # inside _process_with_limit's own finally.
            semaphore = asyncio.Semaphore(n)
            tasks = []
            start = time.monotonic()
            for entry_id, fields in entries:
                await semaphore.acquire()
                tasks.append(
                    asyncio.create_task(
                        agent_worker._process_with_limit(client, entry_id, fields, semaphore)
                    )
                )
            await asyncio.gather(*tasks)
            elapsed = time.monotonic() - start

            results = [
                [event async for event in queue.read_results(client, rid)] for rid in request_ids
            ]
            return elapsed, results

        elapsed, results = run_with_checkpointer_cleanup(_run)

        for events in results:
            assert events[-1]["type"] == "done", events

        # A single turn run alone takes roughly 5 chunks * per_chunk_delay.
        # Run serially, n of them would take about n times that. A relative
        # threshold (not a fixed absolute one) keeps this robust to a slow
        # CI machine while still failing hard if concurrency silently
        # regresses back to one-at-a-time. 0.8, not something tighter like
        # 0.6: verified directly that a tighter margin produced a real
        # (if rare) false failure under `pytest -n auto` specifically —
        # several xdist workers plus this file's OWN other real-Postgres/
        # Redis-backed tests genuinely competing for this machine's CPU at
        # the same moment can slow the concurrent run enough to blow a
        # tight margin even though nothing had actually re-serialized. A
        # true regression back to one-at-a-time would still overshoot even
        # this wider bound by a large margin (5 turns fully serial is
        # ~1.0s vs. this bound's ~4.0s at n=5), so 0.8 stays a real,
        # meaningful guard, just a less trigger-happy one.
        single_turn_estimate = 5 * per_chunk_delay
        serial_estimate = n * single_turn_estimate
        assert elapsed < serial_estimate * 0.8, (
            f"{n} turns took {elapsed:.2f}s wall-clock — looks serialized, not "
            f"concurrent (a single turn alone takes ~{single_turn_estimate:.2f}s, so "
            f"{n} run serially would take ~{serial_estimate:.2f}s)"
        )
