"""Concurrency-correctness suite: does running several turns at once (`agent_worker.py`'s `_MAX_CONCURRENCY` plus
the pooled checkpointer in `app/agent/runtime/core.py`) give correct, isolated results, or corrupt or
cross-deliver state?

Separate from tests/agent/runtime/test_durable_checkpoint.py (real Postgres, fake LLM), whose concurrency
tests cover one specific race (a stale resume hijacking a streaming turn). This asks the general question
under real concurrent load. It runs in CI (`make test-integration`, `@pytest.mark.integration`) against
ephemeral Postgres + Redis Stack containers (tests/containers.py) and a fake LLM (`GenericFakeChatModel`),
with no live Ollama/LiteLLM.

One class per angle:
- TestNoCrossContaminationUnderConcurrency: N turns on N THREADS against the real pooled checkpointer; does
  each history contain ONLY its own message?
- TestSemanticCacheIsolationUnderConcurrency: two TENANTS racing an identical question; a same-tenant repeat
  must hit, the other tenant's first ask must miss.
- TestWorkerConcurrencyAgainstTheRealQueue: the production dispatch (`agent_worker._process_with_limit`)
  against a real Redis queue; do N slow turns genuinely overlap in wall-clock time? The guard against
  reintroduced serial processing, which would still complete "correctly".
- TestHITLApprovalUnderConcurrency: N turns pause at the approval gate (a `remember` call, pattern 15), then
  all resume at once; each resume must unblock ITS OWN call and the memory write must land in Qdrant for the
  right owner.
- TestSubagentCallUnderConcurrency: N turns delegate to the real `run_subagent` (pattern 46), each a nested
  `build_graph()` run on the shared singleton; each must answer ITS OWN task.
- TestQdrantReadWriteUnderConcurrency: N TENANTS searching a seeded collection must hit only their own
  documents (the semantic-cache question asked of Qdrant's tenant filter).
- TestSameThreadTurnsAreSerializedNotCorrupted: the complement; the same thread_id must not fork its
  checkpoint when two "turn" jobs race through the real queue. Before `queue.py::acquire_thread_lock` the last
  commit silently won and the other message vanished; no class above put two jobs on one thread_id.

No embedding backend is needed (`_fake_embed_text`): the real `embed_text` needs a reachable OPENAI_API_BASE,
which `test-integration` CI doesn't provision (a CI run once surfaced that). These tests check tenant
filtering, HITL routing and delegation, never embedding QUALITY, so a deterministic network-free stand-in is
more correct, not just CI-safe.
"""
import asyncio
import hashlib
import itertools
import time
import uuid
from collections.abc import Callable

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.agent.graph.build import GraphDeps
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module
from app.agent.tools import builtin as tools_module
from app.agent.tools import run_subagent as subagent_tools
from app.core import metrics
from app.domains.ecorp import store as ecorp_store
from app.job_queue import agent_worker, queue
from app.retrieval import embeddings as embeddings_module
from app.retrieval import qdrant_store, semantic_cache
from app.retrieval.embedding_identity import EmbeddingIdentity
from tests.conftest import metric_value as _count
from tests.containers import ensure_postgres, ensure_qdrant, ensure_redis
from tests.ecorp import build_graph

pytestmark = pytest.mark.integration


def _ctx(tenant: str, principal: str = "test-user") -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


async def _no_op_search(query: str, ctx) -> tuple[str, list[dict]]:
    """Stubs retrieval only — these tests are about concurrency isolation
    in the checkpointer/cache/queue, not retrieval quality, so this keeps
    the suite from needing a real Qdrant container too."""
    return "", []


def _fixed_llm(text: str = "A sufficiently long final answer.", delay: float = 0.0):
    """A GenericFakeChatModel answering EVERY call with the same `text` (itertools.repeat never exhausts) — these
    tests care whether turns interfere with EACH OTHER, not whether each gets a distinct scripted reply, so
    one fixed response is enough.
    """

    class _Model(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
                if delay:
                    await asyncio.sleep(delay)

    return _Model(messages=itertools.repeat(AIMessage(content=text)))


def _last_human_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _echoing_llm(respond: Callable[[list[BaseMessage]], AIMessage]):
    """A GenericFakeChatModel whose response is DERIVED from the incoming messages (via `respond`) instead of
    played back from a fixed queue.
    """

    class _Model(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=respond(messages))])

        def bind_tools(self, tools, **kwargs):
            return self

    return _Model(messages=iter([]))  # unused — _generate is fully overridden above


@pytest.fixture(autouse=True)
def reset_agent_singleton(monkeypatch):
    """Same fixture as tests/agent/runtime/test_durable_checkpoint.py — see that file's docstring for why
    `_graph`/`_checkpointer_pool`/`_graph_lock` (module-level singletons) need resetting before and after
    every test here too — this file's own `asyncio.gather`-heavy tests (every class below runs several turns
    concurrently) are exactly the shape most likely to leave `_graph_lock` stuck if a gathered task were ever
    torn down while holding it, so this reset matters here at least as much as in that file.
    """
    monkeypatch.setattr(
        agent_module, "CHECKPOINTER_DATABASE_URL", ensure_postgres()["checkpointer_database_url"]
    )
    agent_module._graph = None
    agent_module._checkpointer_pool = None
    agent_module._graph_lock = asyncio.Lock()
    subagent_tools.reset_subagent_graph_cache()
    yield
    agent_module._graph = None
    agent_module._checkpointer_pool = None
    agent_module._graph_lock = asyncio.Lock()
    subagent_tools.reset_subagent_graph_cache()


async def run_with_checkpointer_cleanup(coro_fn):
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

    return await _wrapped()


async def _fake_identity():
    """The identity the semantic cache sizes its Redis index from. Dimension 32 to match `_fake_embed_text`'s sha256-digest vectors;
    the autouse identity in tests/conftest.py says 3, which would make Redis reject every 32-float vector."""
    return EmbeddingIdentity("embed", "test/fake-sha256", 32, "", "", "")


async def _fake_embed_text(text: str) -> list[float]:
    """Deterministic, network-free stand-in for the real `embed_text` (app/retrieval/embeddings.py), which needs
    a reachable OPENAI_API_BASE: fine on a dev machine with `make up`, but absent from the `test-integration`
    CI job, which provisions no LLM.
    """
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest]


@pytest.fixture(autouse=True)
async def real_semantic_cache_redis(monkeypatch):
    """Points the REAL app/retrieval/semantic_cache.py (not stubbed via `GraphDeps.cache_get/cache_set`, since
    isolation under concurrency is what TestSemanticCacheIsolationUnderConcurrency checks) at an ephemeral
    Redis Stack container. `_client`/`_index_ready` are reset as tests/integration/test_queue_real_redis.py
    resets its lazy-singleton client, and `embed_text` is replaced by `_fake_embed_text` (see its docstring).

    `flushall()` matters here: `ensure_redis()`'s container is SHARED and PERSISTENT, reused by every test in
    the session and verified to survive across separate process invocations (a debug script's cache write was
    readable in a fresh python run). semantic_cache.py's index name and key prefix are fixed module constants,
    not per-test like the queue's stream/group names (which the other file scopes to a fresh uuid), so a flush
    is the only way to start the isolation assertions from an empty cache instead of depending on what an
    earlier test or whole run left behind.
    """
    info = ensure_redis()
    monkeypatch.setattr(semantic_cache, "REDIS_URL", info["redis_url"])
    monkeypatch.setattr(semantic_cache, "_client", None)
    monkeypatch.setattr(semantic_cache, "_ready_indexes", set())
    monkeypatch.setattr(semantic_cache, "embed_query", _fake_embed_text)
    monkeypatch.setattr(semantic_cache.embedding_identity, "current_identity", _fake_identity)
    await semantic_cache._get_client().flushall()
    yield


async def _real_search_docs(query: str, ctx) -> tuple[str, list[dict]]:
    """The REAL retrieval path (app/agent/graph/build.py::default_search's own wrapper over
    app/agent/tools/builtin.py::gather_context, reproduced here since that real default is exactly what
    tests/conftest.py's session-wide `mock_search_docs` fixture overrides — see `_install_fake_graph`'s own
    docstring for the identical reasoning applied to the semantic cache).
    """
    return await tools_module.gather_context(ctx, query)


def _install_fake_graph(monkeypatch, llm, *, search_docs=_no_op_search) -> None:
    """`cache_get`/`cache_set` are wired explicitly to the REAL app/retrieval/semantic_cache.py module functions
    — GraphDeps's own documented test-injection seam (see its docstring: "tests set individual fields to
    inject fakes instead of monkeypatching module globals").
    """
    monkeypatch.setattr(
        agent_module,
        "build_graph",
        lambda manifest, domain, checkpointer=None: build_graph(
            GraphDeps(
                llm=llm,
                search_docs=search_docs,
                cache_get=semantic_cache.get,
                cache_set=semantic_cache.set,
            ),
            checkpointer=checkpointer,
        ),
    )


_FAKE_EMBED_DIM = 32  # len(hashlib.sha256(...).digest()) — see _fake_embed_text


@pytest.fixture
async def real_qdrant(monkeypatch):
    """Point every real Qdrant read/write path (app/agent/tools/builtin.py's remember/add_note/search_docs, down
    through app/retrieval/qdrant_store.py) at a real, ephemeral Qdrant container. For
    TestHITLApprovalUnderConcurrency and TestQdrantReadWriteUnderConcurrency specifically, not autouse: the
    other classes never touch Qdrant.

    `embed_text` is patched in BOTH places that hold a separate binding of it: app/agent/tools/builtin.py's
    `from ... import embed_text`, and app/retrieval/embeddings.py itself (qdrant_store.hybrid_search reads it
    late via `from app.retrieval import embeddings; embeddings.embed_text(...)`, so patching the ORIGIN module
    is what that call site needs). A `from X import Y` binding is a separate reference, the same reasoning
    `real_semantic_cache_redis` applies to semantic_cache.py's copy.

    `COLLECTION` is also patched to a unique name per test, and that is not optional hygiene:
    `ensure_qdrant()`'s container is shared across the whole pytest session and, under `pytest -n auto`,
    across every xdist worker (what CI runs). This file's `ensure_collection()` (a DESTRUCTIVE
    `recreate_collection`) and tests/integration/test_worker_scaling.py's identical call raced on the SAME
    default `COLLECTION` ("docs") on different workers and wiped each other's seeded data mid-test: one run
    saw no citations from its own just-seeded document, another a stray memory from a DIFFERENT file's
    same-named principal. A unique name per test is the idiom tests/integration/test_queue_real_redis.py's
    `real_redis` fixture applies to REQUESTS_STREAM/CONSUMER_GROUP.
    """
    qdrant = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", qdrant["qdrant_url"])
    monkeypatch.setattr(qdrant_store, "COLLECTION", f"test-concurrent-turns-{uuid.uuid4().hex}")
    monkeypatch.setattr(ecorp_store, "embed_document", _fake_embed_text)
    monkeypatch.setattr(embeddings_module, "embed_query", _fake_embed_text)
    await qdrant_store.ensure_collection(dim=_FAKE_EMBED_DIM)
    yield


class TestNoCrossContaminationUnderConcurrency:
    """The pooled checkpointer (app/agent/runtime/core.py::_open_checkpointer)
    replaced a single shared AsyncConnection specifically so concurrent
    turns don't all funnel through one connection — this is the most
    direct possible check that the pool genuinely ISOLATES them rather
    than cross-delivering state: N turns on N different threads, run truly
    concurrently, each thread's checkpointed history must end up
    containing ONLY its own message."""

    async def test_n_concurrent_turns_on_different_threads_keep_separate_checkpointed_state(
        self, monkeypatch
    ):
        n = 8
        _install_fake_graph(monkeypatch, _fixed_llm())

        async def _run():
            graph = await agent_module.init_graph_async()
            thread_ids = [str(uuid.uuid4()) for _ in range(n)]

            async def drive(i: int) -> None:
                async for _ in stream_module.astream_events_turn(
                    f"remember the number {i}", thread_ids[i], _ctx(f"tenant-{i}")
                ):
                    pass

            await asyncio.gather(*(drive(i) for i in range(n)))

            return [
                await graph.aget_state({"configurable": {"thread_id": tid}}) for tid in thread_ids
            ]

        states = await run_with_checkpointer_cleanup(_run)

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

    async def test_a_different_tenants_first_ask_never_hits_another_tenants_cached_entry(
        self, monkeypatch
    ):
        _install_fake_graph(monkeypatch, _fixed_llm())
        question = "What is a LangGraph checkpointer?"

        async def drive(tenant: str) -> None:
            async for _ in stream_module.astream_events_turn(question, str(uuid.uuid4()), _ctx(tenant)):
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

        hits_before, misses_before = await run_with_checkpointer_cleanup(_run)

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
    """Regression guard for the actual production dispatch code (app/job_queue/agent_worker.py::run's
    Semaphore-gated asyncio.create_task loop, exercised here via the same `_process_with_limit` helper `run()`
    itself calls) against a real Redis queue.
    """

    @pytest.fixture(autouse=True)
    def real_redis_for_queue(self, monkeypatch):
        """Same isolation technique as
        tests/integration/test_queue_real_redis.py's own `real_redis`
        fixture, applied to BOTH `queue`'s and `agent_worker`'s own
        requests-stream/`CONSUMER_GROUP` bindings — `agent_worker.py` binds
        its own `REQUESTS_STREAM` module global once at import time (see
        that module's own docstring on why: domain is a per-PROCESS
        property), a separate reference `queue.requests_stream_key`'s own
        rebinding below doesn't reach, so it needs its own direct patch;
        `process_request`'s `finally: client.xack(...)` reads that global,
        not a parameter. `queue.requests_stream_key` is patched (not a
        fixed constant, now that it's domain-parameterized) to always
        return this same `stream` regardless of domain — this test only
        ever exercises the default ("ecorp") one anyway."""
        info = ensure_redis()
        stream = f"agent:requests:test:{uuid.uuid4()}"
        group = f"agent-workers:test:{uuid.uuid4()}"
        monkeypatch.setattr(queue, "REDIS_URL", info["redis_url"])
        monkeypatch.setattr(queue, "_client", None)
        monkeypatch.setattr(queue, "requests_stream_key", lambda domain="ecorp": stream)
        monkeypatch.setattr(queue, "CONSUMER_GROUP", group)
        monkeypatch.setattr(agent_worker, "REQUESTS_STREAM", stream)
        monkeypatch.setattr(agent_worker, "CONSUMER_GROUP", group)
        yield
        queue._client = None

    async def test_n_queued_turns_run_concurrently_not_back_to_back(self, monkeypatch):
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

            # The EXACT dispatch shape app/job_queue/agent_worker.py::run uses:
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

        elapsed, results = await run_with_checkpointer_cleanup(_run)

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


class TestSameThreadTurnsAreSerializedNotCorrupted:
    """The complementary case to every OTHER class in this file: those all
    put N turns on N DIFFERENT thread_ids/tenants and check they stay
    isolated. This puts TWO real "turn" jobs on the SAME thread_id through
    the real queue/worker dispatch at the same real moment — before
    `app/job_queue/queue.py::acquire_thread_lock` existed, both would read
    the checkpointer's latest state as their own parent and both write a
    child from it, so whichever committed last would silently win and the
    other job's message would vanish from the thread's visible history.
    Proves the fix: exactly one job runs (the other is rejected fast with
    `ErrorCode.THREAD_BUSY`, never reaching the graph at all), and the
    checkpointed thread ends up with exactly the winner's own message —
    never zero (both rejected), never two (a fork)."""

    @pytest.fixture(autouse=True)
    def real_redis_for_queue(self, monkeypatch):
        """Same isolation technique as
        TestWorkerConcurrencyAgainstTheRealQueue's own fixture above — see
        that one's docstring for why each binding needs its own patch."""
        info = ensure_redis()
        stream = f"agent:requests:test:{uuid.uuid4()}"
        group = f"agent-workers:test:{uuid.uuid4()}"
        monkeypatch.setattr(queue, "REDIS_URL", info["redis_url"])
        monkeypatch.setattr(queue, "_client", None)
        monkeypatch.setattr(queue, "requests_stream_key", lambda domain="ecorp": stream)
        monkeypatch.setattr(queue, "CONSUMER_GROUP", group)
        monkeypatch.setattr(agent_worker, "REQUESTS_STREAM", stream)
        monkeypatch.setattr(agent_worker, "CONSUMER_GROUP", group)
        yield
        queue._client = None

    async def test_two_concurrent_turns_on_one_thread_one_wins_one_is_rejected(self, monkeypatch):
        # delay=0.05 (same technique as TestWorkerConcurrencyAgainstTheRealQueue
        # above) keeps the winning job's handler genuinely in flight long
        # enough for the loser's acquire_thread_lock call to land while
        # the lock is still held — without it, both jobs could complete so
        # fast the race window never actually opens.
        _install_fake_graph(monkeypatch, _fixed_llm("A sufficiently long final answer.", delay=0.05))

        async def _run():
            graph = await agent_module.init_graph_async()
            client = queue.get_client()
            await queue.ensure_consumer_group(client)

            thread_id = str(uuid.uuid4())
            request_ids = [str(uuid.uuid4()) for _ in range(2)]
            for i, rid in enumerate(request_ids):
                await queue.publish_request(
                    client,
                    request_id=rid,
                    text=f"message {i}",
                    thread_id=thread_id,
                    ctx=_ctx("tenant-same-thread"),
                )

            response = await client.xreadgroup(
                agent_worker.CONSUMER_GROUP,
                "test-consumer",
                {agent_worker.REQUESTS_STREAM: ">"},
                count=2,
            )
            _, entries = response[0]
            assert len(entries) == 2

            # The exact dispatch shape agent_worker.py::run uses — see
            # TestWorkerConcurrencyAgainstTheRealQueue above for why.
            semaphore = asyncio.Semaphore(2)
            tasks = []
            for entry_id, fields in entries:
                await semaphore.acquire()
                tasks.append(
                    asyncio.create_task(
                        agent_worker._process_with_limit(client, entry_id, fields, semaphore)
                    )
                )
            await asyncio.gather(*tasks)

            results = {
                rid: [event async for event in queue.read_results(client, rid)]
                for rid in request_ids
            }
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return results, state

        results, state = await run_with_checkpointer_cleanup(_run)

        outcomes = {rid: events[-1] for rid, events in results.items()}
        done_count = sum(1 for e in outcomes.values() if e["type"] == "done")
        busy_count = sum(1 for e in outcomes.values() if e.get("code") == "thread_busy")
        assert done_count == 1, outcomes
        assert busy_count == 1, outcomes

        # The checkpoint was never forked: exactly the WINNING turn's own
        # message made it into the thread's visible history — a fork would
        # show up here as either 2 (both jobs' messages, on divergent
        # branches merged by this read) or, in other orderings, the
        # loser's message instead of the winner's. Either way, "exactly
        # one, and it's a real message" is the only correct outcome.
        human_texts = [m.content for m in state.values["messages"] if isinstance(m, HumanMessage)]
        assert len(human_texts) == 1, (
            f"expected exactly the winning turn's own message, got {human_texts} — "
            "a checkpoint fork would show up here as something other than exactly one"
        )
        assert human_texts[0] in {"message 0", "message 1"}


class TestHITLApprovalUnderConcurrency:
    """`remember` is "mutating" (app/domains/ecorp/tools.py::TOOL_CAPABILITIES), so it ALWAYS pauses at the
    human_approval gate whether or not the caller opted into require_approval (the MANDATORY gate of
    GRAPH_PATTERNS.md pattern 15, not the opt-in one).
    """

    async def test_n_concurrent_pauses_and_resumes_stay_matched_to_their_own_thread(
        self, monkeypatch, real_qdrant
    ):
        n = 6
        tenant = "tenant-hitl"

        def _respond(messages: list[BaseMessage]) -> AIMessage:
            last = messages[-1]
            if isinstance(last, ToolMessage):
                # `remember`'s real return value is the fixed string
                # "Remembered." (app/domains/ecorp/store.py::remember_impl), not an
                # echo of what was remembered — the ORIGINAL human text is
                # what actually distinguishes this thread from a sibling's,
                # and it's still the only HumanMessage in this short
                # conversation on this second call too.
                return AIMessage(content=f"Noted: {_last_human_text(messages)}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remember",
                        "args": {"content": _last_human_text(messages)},
                        "id": f"call-{uuid.uuid4().hex[:8]}",
                    }
                ],
            )

        _install_fake_graph(monkeypatch, _echoing_llm(_respond))

        async def pause(i: int, graph) -> tuple[str, str]:
            thread_id = str(uuid.uuid4())
            principal = f"user-{i}"
            cfg = {"configurable": {"thread_id": thread_id, "ctx": _ctx(tenant, principal)}}
            await graph.ainvoke(
                {"messages": [HumanMessage(content=f"please remember distinguishing fact {i}")]},
                config=cfg,
            )
            return thread_id, principal

        async def resume(thread_id: str, principal: str) -> list:
            events = []
            async for event in stream_module.astream_events_resume(
                thread_id, True, _ctx(tenant, principal)
            ):
                events.append(event)
            return events

        async def _run():
            graph = await agent_module.init_graph_async()
            paused = await asyncio.gather(*(pause(i, graph) for i in range(n)))
            for i, (thread_id, _) in enumerate(paused):
                state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                assert state.next, f"thread {i} should be paused at human_approval, but wasn't"
            resumed = await asyncio.gather(
                *(resume(thread_id, principal) for thread_id, principal in paused)
            )
            return resumed

        resumed = await run_with_checkpointer_cleanup(_run)

        for i, events in enumerate(resumed):
            assert events[-1]["type"] == "done", events
            answer = "".join(e["content"] for e in events if e["type"] == "token")
            assert f"distinguishing fact {i}" in answer, (
                f"thread {i}'s resume answer didn't reflect its own approved "
                f"memory — got {answer!r}"
            )
            for j in range(n):
                if j != i:
                    assert f"distinguishing fact {j}" not in answer, (
                        f"thread {i}'s resume answer leaked thread {j}'s memory: {answer!r}"
                    )

        # The real write underneath the approval, per thread: exactly one
        # memory, owned by the right principal, with the right text.
        for i in range(n):
            principal = f"user-{i}"
            points, _ = await qdrant_store.get_client().scroll(
                collection_name=qdrant_store.live_name(),  # the app writes through the `-live` alias; the logical name is not a collection
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="kind", match=MatchValue(value="memory")),
                        FieldCondition(key="owner", match=MatchValue(value=principal)),
                    ]
                ),
                limit=10,
            )
            assert len(points) == 1, f"expected exactly 1 memory for {principal}, got {len(points)}"
            memory_text = points[0].payload["text"]
            assert f"distinguishing fact {i}" in memory_text, memory_text
            for j in range(n):
                if j != i:
                    assert f"distinguishing fact {j}" not in memory_text, (
                        f"{principal}'s memory leaked thread {j}'s fact: {memory_text!r}"
                    )
            assert points[0].payload["tenant"] == tenant


class TestSubagentCallUnderConcurrency:
    """The real `run_subagent` tool (read_only — no HITL pause, GRAPH_PATTERNS.md pattern 46) delegates to a
    genuinely separate, nested `build_graph()` run per call
    (app/agent/tools/run_subagent.py::run_subagent_impl) — its own fresh messages (the delegated task, NOT
    this conversation's history), its own LLM client construction.
    """

    async def test_n_concurrent_subagent_delegations_never_cross_wire(self, monkeypatch):
        n = 6

        def _nested_respond(messages: list[BaseMessage]) -> AIMessage:
            # run_subagent_impl's own docstring: the nested run's ONLY
            # human message is the delegated task itself, never the
            # parent's conversation history.
            return AIMessage(content=f"Nested result for: {_last_human_text(messages)}")

        monkeypatch.setattr(
            subagent_tools, "make_llm", lambda *args, **kwargs: _echoing_llm(_nested_respond)
        )

        def _top_respond(messages: list[BaseMessage]) -> AIMessage:
            last = messages[-1]
            if isinstance(last, ToolMessage):
                return AIMessage(content=f"Subagent said: {last.content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_subagent",
                        "args": {
                            "subagent_name": "researcher",
                            "task": _last_human_text(messages),
                        },
                        "id": f"call-{uuid.uuid4().hex[:8]}",
                    }
                ],
            )

        _install_fake_graph(monkeypatch, _echoing_llm(_top_respond))

        async def drive(i: int, graph) -> str:
            cfg = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": _ctx(f"tenant-{i}")}}
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=f"delegate task {i}")]}, config=cfg
            )
            return result["messages"][-1].content

        async def _run():
            graph = await agent_module.init_graph_async()
            return await asyncio.gather(*(drive(i, graph) for i in range(n)))

        answers = await run_with_checkpointer_cleanup(_run)

        for i, answer in enumerate(answers):
            assert f"delegate task {i}" in answer, (
                f"thread {i}'s final answer didn't reflect its own delegated "
                f"task's nested result — got {answer!r}"
            )
            for j in range(n):
                if j != i:
                    assert f"delegate task {j}" not in answer, (
                        f"thread {i}'s answer leaked thread {j}'s delegated task: {answer!r}"
                    )

    async def test_two_parallel_subagent_calls_in_one_turn_both_land_in_subagent_spend(
        self, monkeypatch
    ):
        """Different axis than the test above: THAT one proves N separate
        concurrent TURNS never cross-wire; this proves TWO run_subagent
        calls issued in a SINGLE AI turn (ToolNode already runs a batch of
        tool calls concurrently, GRAPH_PATTERNS.md pattern 9) each safely
        contribute their own entry to `subagent_spend` — the
        `Annotated[list, operator.add]` reducer merging two concurrent
        `Command(update={"subagent_spend": [...]})` writes within the same
        graph superstep, not racing/clobbering each other the way a plain
        overwrite field would."""
        monkeypatch.setattr(
            subagent_tools,
            "make_llm",
            lambda *args, **kwargs: _echoing_llm(lambda msgs: AIMessage(content="nested done")),
        )

        def _top_respond(messages: list[BaseMessage]) -> AIMessage:
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            if len(tool_messages) >= 2:
                return AIMessage(content="Both subagents reported back.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_subagent",
                        "args": {"subagent_name": "researcher", "task": f"lookup {i}"},
                        "id": f"call-{i}-{uuid.uuid4().hex[:8]}",
                    }
                    for i in range(2)
                ],
            )

        _install_fake_graph(monkeypatch, _echoing_llm(_top_respond))

        async def _run():
            graph = await agent_module.init_graph_async()
            cfg = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": _ctx("tenant-x")}}
            return await graph.ainvoke(
                {"messages": [HumanMessage(content="delegate two lookups")]}, config=cfg
            )

        result = await run_with_checkpointer_cleanup(_run)

        assert len(result["subagent_spend"]) == 2


class TestQdrantReadWriteUnderConcurrency:
    """app/retrieval/qdrant_store.py's hybrid_search scopes every read to
    ctx['tenant'] as a server-side Filter predicate
    (app/core/security.py::Policy.lower), ANDed onto both the dense AND
    sparse prefetch legs — the same tenant pre-filter discipline
    TestSemanticCacheIsolationUnderConcurrency already proves for Redis's
    own TAG filter, asked here of Qdrant's instead: N different tenants
    concurrently searching the SAME real, shared collection must each
    surface ONLY their own seeded document, regardless of embedding/rerank
    specifics — the filter narrows the CANDIDATE SET itself, at the query
    level, not just the final ranking."""

    async def test_n_tenants_concurrent_search_never_crosses_into_a_siblings_documents(
        self, monkeypatch, real_qdrant
    ):
        n = 6
        for i in range(n):
            text = f"distinguishing content {i}"
            point = qdrant_store.build_point(
                point_id=str(uuid.uuid4()),
                dense_vector=await _fake_embed_text(text),
                payload={
                    "text": text,
                    "topic": "company",
                    "title": f"doc-{i}",
                    "kind": "document",
                    "tenant": f"tenant-{i}",
                },
            )
            await qdrant_store.upsert([point])

        _install_fake_graph(monkeypatch, _fixed_llm(), search_docs=_real_search_docs)

        async def drive(i: int, graph) -> list[dict]:
            thread_id = str(uuid.uuid4())
            async for _ in stream_module.astream_events_turn(
                f"distinguishing content {i}", thread_id, _ctx(f"tenant-{i}")
            ):
                pass
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.values.get("citations") or []

        async def _run():
            graph = await agent_module.init_graph_async()
            return await asyncio.gather(*(drive(i, graph) for i in range(n)))

        all_citations = await run_with_checkpointer_cleanup(_run)

        for i, citations in enumerate(all_citations):
            assert citations, f"tenant-{i} got no citations at all from its own real Qdrant search"
            for citation in citations:
                assert citation["title"] == f"doc-{i}", (
                    f"tenant-{i}'s search surfaced a citation that isn't its own "
                    f"document: {citation}"
                )
                assert citation["text"] == f"distinguishing content {i}"
