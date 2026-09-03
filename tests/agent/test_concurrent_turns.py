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

Six angles, one per test class below:
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
- TestHITLApprovalUnderConcurrency: N turns all pause at the mandatory
  human_approval gate (a `remember` call, GRAPH_PATTERNS.md pattern 15) at
  the same real moment, then all N resume concurrently — does each
  thread's resume correctly unblock ITS OWN paused tool call (not a
  concurrent sibling's), and does the real memory write it approves land
  in Qdrant correctly scoped to the right owner?
- TestSubagentCallUnderConcurrency: N turns all delegate to the real
  `run_subagent` tool (GRAPH_PATTERNS.md pattern 46) at the same real
  moment — a genuinely separate nested `build_graph()` run per call, on
  the same shared graph singleton everything else here uses — does each
  turn's own nested run answer ITS OWN delegated task, never a sibling's?
- TestQdrantReadWriteUnderConcurrency: N different TENANTS concurrently
  searching a real, pre-seeded Qdrant collection — does each tenant's
  real hybrid-search hit ONLY ITS OWN documents, the same tenant-isolation
  question TestSemanticCacheIsolationUnderConcurrency asks of Redis, asked
  here of Qdrant's own tenant-scoped filter instead?

A real, reachable embedding backend is deliberately never required for any
of this (`_fake_embed_text` below) — every real embedding call in this app
(app/retrieval/embeddings.py::embed_text) needs a reachable
OPENAI_API_BASE, which `test-integration` CI deliberately doesn't
provision (see tests/integration/'s own "no LLM" scope) — a real CI run
surfaced exactly this gap once already (see this file's own git history);
what these tests actually check — RediSearch/Qdrant's own tenant-scoped
filtering, HITL pause/resume routing, subagent delegation — was never
about embedding QUALITY, so a deterministic, network-free stand-in is the
more correct choice here, not just a CI-safety workaround.
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

from app.agent import runtime as agent_module
from app.agent import tools as tools_module
from app.agent.graph import GraphDeps, build_graph
from app.core import metrics
from app.core.config import COLLECTION
from app.retrieval import embeddings as embeddings_module
from app.retrieval import qdrant_store, semantic_cache
from app.turns import agent_worker, queue
from tests.conftest import metric_value as _count
from tests.containers import ensure_postgres, ensure_qdrant, ensure_redis

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


def _last_human_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _echoing_llm(respond: Callable[[list[BaseMessage]], AIMessage]):
    """A GenericFakeChatModel whose response is DERIVED from the incoming
    messages (via `respond`) instead of played back from a fixed queue.

    `_fixed_llm` above is enough when every concurrent turn should behave
    IDENTICALLY (proving state isolation despite identical LLM behavior).
    HITL and subagent-delegation correctness need the opposite: THIS
    process's whole graph is one shared singleton, built once — every
    concurrent turn's LLM calls land on the SAME model instance, so
    proving turn A's own tool call/final answer never gets crossed with
    concurrent turn B's requires a response that's actually a function of
    turn A's own input, not a canned reply every turn would get alike.

    Overrides `_generate` only (not `_astream` directly) — GenericFakeChatModel's
    own inherited `_stream`/async-streaming machinery already turns
    whatever `AIMessage` `_generate` returns (including `tool_calls`) into
    correctly-shaped streaming chunks, the same machinery the existing
    fixed-queue tests already rely on; overriding only the RESPONSE
    SELECTION here, not that plumbing, keeps this consistent with them.

    Also overrides `bind_tools` to a no-op returning `self` —
    `BaseChatModel`'s own default raises `NotImplementedError` (verified
    directly), which `GraphDeps(llm=...)`'s own top-level use of a fake
    model never trips (`build_graph()` uses `deps.llm` raw, never calling
    `.bind_tools()` on it — only its OWN `_make_llm()` production
    construction path does that), but
    app/agent/tools.py::_run_subagent_impl's fallback DOES call
    `ChatOpenAI(...).bind_tools(nested_tools)` on whatever `ChatOpenAI`
    constructs — needed for TestSubagentCallUnderConcurrency, which
    monkeypatches `ChatOpenAI` itself to return one of these. Same
    reasoning `_fixed_llm`/every fake model in this file already
    embodies: a scripted response doesn't actually depend on which tools
    were "offered," so a real bind implementation was never load-bearing
    here."""

    class _Model(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=respond(messages))])

        def bind_tools(self, tools, **kwargs):
            return self

    return _Model(messages=iter([]))  # unused — _generate is fully overridden above


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


def _fake_embed_text(text: str) -> list[float]:
    """Deterministic, network-free stand-in for the REAL embed_text
    (app/retrieval/embeddings.py), which needs a reachable OpenAI-compatible
    endpoint (OPENAI_API_BASE) — fine on a dev machine with `make up`
    running, but NOT available in the `test-integration` CI job, which
    deliberately provisions no LLM at all (see tests/integration/'s own
    "no LLM" scope). Verified directly: a real CI run hit exactly this gap
    — every embed_text() call failed, semantic_cache.get/set's own
    degrade-to-miss `except Exception` swallowed it silently, so
    TestSemanticCacheIsolationUnderConcurrency's "must hit" assertion
    failed for a completely different reason than the isolation bug it
    exists to catch. tests/live/conftest.py's own module docstring
    documents hitting this identical class of gap before (test_qdrant_real.py
    started in tests/integration/ too, moved once a real CI run surfaced
    `openai.APIConnectionError`).

    What THIS file's tests actually exercise is semantic_cache.py's
    TAG-scoped RediSearch KNN query — same text -> same vector (so a
    same-tenant repeat can hit) and isolation enforced by the tenant/
    principal TAG filter regardless of the vector's actual values — a real
    embedding model was never load-bearing for that, so a deterministic
    hash-based vector is a correct, not just convenient, substitute here."""
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest]


@pytest.fixture(autouse=True)
def real_semantic_cache_redis(monkeypatch):
    """Points the REAL app/retrieval/semantic_cache.py module (not stubbed
    via GraphDeps.cache_get/cache_set — isolation under concurrency is
    exactly what TestSemanticCacheIsolationUnderConcurrency exists to
    check) at a real, ephemeral Redis Stack container. `_client`/
    `_index_ready` reset the same way tests/integration/test_queue_real_redis.py
    resets its own module-level lazy-singleton client. `embed_text` is
    replaced with `_fake_embed_text` — see that function's own docstring
    for why a real embedding model was never actually needed here, and is
    unavailable in CI anyway.

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
    monkeypatch.setattr(semantic_cache, "embed_text", _fake_embed_text)
    semantic_cache._get_client().flushall()
    yield


def _real_search_docs(query: str, ctx) -> tuple[str, list[dict]]:
    """The REAL retrieval path (app/agent/graph.py::_default_search's own
    wrapper over app/agent/tools.py::gather_context, reproduced here since
    that real default is exactly what tests/conftest.py's session-wide
    `mock_search_docs` fixture overrides — see `_install_fake_graph`'s own
    docstring for the identical reasoning applied to the semantic cache).
    Parameter order matches `_default_search`'s own adaptation:
    `gather_context` takes `(ctx, query)`, reversed from what
    `retrieve_context`/`GraphDeps.search_docs` call with."""
    return tools_module.gather_context(ctx, query)


def _install_fake_graph(monkeypatch, llm, *, search_docs=_no_op_search) -> None:
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
    hardcoded miss, not a real Redis round trip).

    `search_docs` defaults to `_no_op_search` (matching every OTHER class
    in this file, which isn't testing retrieval) — pass `_real_search_docs`
    for TestQdrantReadWriteUnderConcurrency specifically, for the exact
    same "the global mock would otherwise silently win" reason above,
    applied to app/agent/graph.py's `_default_search` instead of its cache
    counterpart."""
    monkeypatch.setattr(
        agent_module,
        "build_graph",
        lambda checkpointer=None, manifest=None, domain=None: build_graph(
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
def real_qdrant(monkeypatch):
    """Points every real Qdrant read/write path this app has
    (app/agent/tools.py's remember/add_note/search_docs, all the way down
    through app/retrieval/qdrant_store.py) at a real, ephemeral Qdrant
    container — for TestHITLApprovalUnderConcurrency and
    TestQdrantReadWriteUnderConcurrency specifically, not autouse, since
    the other classes in this file never touch Qdrant at all.

    `embed_text` is patched in BOTH places that hold their own separate
    binding of it (app/agent/tools.py's `from ... import embed_text`, and
    app/retrieval/embeddings.py itself — read via a late `from
    app.retrieval import embeddings; embeddings.embed_text(...)` inside
    qdrant_store.hybrid_search, so patching the ORIGIN module is what that
    one particular call site actually needs) — same "a `from X import Y`
    binding is a separate reference" reasoning this file's own
    `real_semantic_cache_redis` fixture already applies to
    app/retrieval/semantic_cache.py's own copy of the same name."""
    qdrant = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", qdrant["qdrant_url"])
    monkeypatch.setattr(tools_module, "embed_text", _fake_embed_text)
    monkeypatch.setattr(embeddings_module, "embed_text", _fake_embed_text)
    qdrant_store.ensure_collection(dim=_FAKE_EMBED_DIM)
    yield


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


class TestHITLApprovalUnderConcurrency:
    """`remember` is declared "mutating" (app/agent/tools.py::TOOL_CAPABILITIES),
    so it ALWAYS pauses at the human_approval gate regardless of whether
    the caller opted into require_approval (GRAPH_PATTERNS.md pattern
    15's MANDATORY gate, not the opt-in one). Proves the pause/resume
    round trip stays correctly matched to its own thread under real
    concurrency: N turns all pause at the same real moment, then all N
    resume at the same real moment — a resume that raced onto the wrong
    thread's paused checkpoint would either hijack a sibling's approval or
    corrupt its memory write. Checks both the routing (each resume's own
    final answer echoes its own remembered text, never a sibling's) and
    the real Qdrant write underneath it (each thread's memory point
    exists, correctly owned, with the right text).

    The PAUSE half drives via `graph.ainvoke()`, not `astream_events_turn`
    — verified directly (the same limitation
    tests/agent/test_durable_checkpoint.py's own `TestAsyncSeeding`/
    `TestResumabilityErrorRejectsAnActivelyRunningThread` classes already
    document and work around the identical way): `astream_events()`
    raises "No generations found in stream" for a `.bind_tools()`-wrapped
    `GenericFakeChatModel`'s tool-calling response — a pre-existing
    fake-LLM/streaming fixture limitation, unrelated to what's under test
    here. The RESUME half stays on the real `astream_events_resume`
    streaming path (the same one `POST /chat/resume` actually uses) since
    its own generation, synthesizing after the tool result, is plain text
    and streams fine — the same reason every OTHER test in this file
    streams without issue."""

    def test_n_concurrent_pauses_and_resumes_stay_matched_to_their_own_thread(
        self, monkeypatch, real_qdrant
    ):
        n = 6
        tenant = "tenant-hitl"

        def _respond(messages: list[BaseMessage]) -> AIMessage:
            last = messages[-1]
            if isinstance(last, ToolMessage):
                # `remember`'s real return value is the fixed string
                # "Remembered." (app/agent/tools.py::_remember_impl), not an
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
            async for event in agent_module.astream_events_resume(
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

        resumed = run_with_checkpointer_cleanup(_run)

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
            points, _ = qdrant_store.get_client().scroll(
                collection_name=COLLECTION,
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
    """The real `run_subagent` tool (read_only — no HITL pause,
    GRAPH_PATTERNS.md pattern 46) delegates to a genuinely separate,
    nested `build_graph()` run per call (app/agent/tools.py::_run_subagent_impl)
    — its own fresh messages (the delegated task, NOT this conversation's
    history), its own LLM client construction. Proves N concurrent
    top-level turns' own nested subagent runs never cross-wire: each
    turn's final answer must reflect ONLY its own delegated task.

    Drives via `graph.ainvoke()`, not `astream_events_turn` — same
    "GenericFakeChatModel can't stream a tool-calling response through
    astream_events()" limitation TestHITLApprovalUnderConcurrency's own
    docstring documents (this test's top-level fake LLM ALSO emits a
    tool_call, for `run_subagent`), just with no pause to split around
    here — the whole turn (tool call, nested run, final synthesis) goes
    through `ainvoke()` in one shot."""

    def test_n_concurrent_subagent_delegations_never_cross_wire(self, monkeypatch):
        n = 6

        def _nested_respond(messages: list[BaseMessage]) -> AIMessage:
            # _run_subagent_impl's own docstring: the nested run's ONLY
            # human message is the delegated task itself, never the
            # parent's conversation history.
            return AIMessage(content=f"Nested result for: {_last_human_text(messages)}")

        monkeypatch.setattr(
            tools_module, "ChatOpenAI", lambda *args, **kwargs: _echoing_llm(_nested_respond)
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

        answers = run_with_checkpointer_cleanup(_run)

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

    def test_n_tenants_concurrent_search_never_crosses_into_a_siblings_documents(
        self, monkeypatch, real_qdrant
    ):
        n = 6
        for i in range(n):
            text = f"distinguishing content {i}"
            point = qdrant_store.build_point(
                point_id=str(uuid.uuid4()),
                dense_vector=_fake_embed_text(text),
                payload={
                    "text": text,
                    "topic": "company",
                    "title": f"doc-{i}",
                    "kind": "document",
                    "tenant": f"tenant-{i}",
                },
            )
            qdrant_store.upsert([point])

        _install_fake_graph(monkeypatch, _fixed_llm(), search_docs=_real_search_docs)

        async def drive(i: int, graph) -> list[dict]:
            thread_id = str(uuid.uuid4())
            async for _ in agent_module.astream_events_turn(
                f"distinguishing content {i}", thread_id, _ctx(f"tenant-{i}")
            ):
                pass
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.values.get("citations") or []

        async def _run():
            graph = await agent_module.init_graph_async()
            return await asyncio.gather(*(drive(i, graph) for i in range(n)))

        all_citations = run_with_checkpointer_cleanup(_run)

        for i, citations in enumerate(all_citations):
            assert citations, f"tenant-{i} got no citations at all from its own real Qdrant search"
            for citation in citations:
                assert citation["title"] == f"doc-{i}", (
                    f"tenant-{i}'s search surfaced a citation that isn't its own "
                    f"document: {citation}"
                )
                assert citation["text"] == f"distinguishing content {i}"
