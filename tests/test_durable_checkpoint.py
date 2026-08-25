"""Tests for the durable checkpointer machinery in app/agent.py
(init_graph_sync/init_graph_async) and app/graph.py's resumability_error.

Everything here uses a real AsyncPostgresSaver against docker-compose's
`checkpointer` database (see postgres-init/05-checkpointer-db.sql) — no
Qdrant/LiteLLM — so it stays as close as possible to this suite's "no live
services" contract while actually exercising the real dependency (the
langgraph-checkpoint-postgres/psycopg version pairing is exactly the kind
of thing that breaks silently without a real test, the same reasoning the
original SQLite-backed version of this file used). Unlike a SQLite file
(a bare tmp_path, no service required), a real Postgres server IS an
external dependency this suite otherwise avoids — so every test here is
gated by `_require_postgres`, which skips cleanly (not fails) when it's
unreachable, keeping `pytest -q` green with zero services running while
`make up && pytest -q` gets full real-backend coverage. Test isolation
doesn't need a fresh database per test (unlike the old tmp_path-per-test
SQLite file): every test already scopes its checkpoint rows by a unique
`uuid.uuid4()` thread_id, and AsyncPostgresSaver's tables are keyed by
thread_id — sharing one database across test runs just means old rows
accumulate harmlessly in a throwaway dev database, not a correctness issue.

`app.agent._graph`/`_checkpointer_cm` are module-level singletons; the
`reset_agent_singleton` fixture below resets them before and after every
test in this file so tests don't leak state into each other (or into other
test modules, which never touch app.agent and would otherwise be affected
by whichever checkpointer happened to win the singleton race).
"""
import asyncio
import uuid

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app import agent as agent_module
from app import metrics
from app.config import CHECKPOINTER_DATABASE_URL
from app.graph import (
    STATE_SCHEMA_VERSION,
    GraphDeps,
    build_graph,
    resumability_error,
    resumability_error_async,
)
from tests.conftest import TEST_CTX


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


def _require_postgres() -> None:
    """Skip (not fail) the calling test if the checkpointer database isn't
    reachable — a real connection attempt, not just a TCP probe, so this
    also catches "server's up but the `checkpointer` database itself
    doesn't exist yet" (e.g. an existing docker volume that predates
    postgres-init/05-checkpointer-db.sql, which only runs on a fresh
    volume — see that file's own comment)."""
    try:
        with psycopg.connect(CHECKPOINTER_DATABASE_URL, connect_timeout=2):
            pass
    except Exception as exc:  # noqa: BLE001 - any failure means "skip", not "fail"
        pytest.skip(f"checkpointer Postgres database not reachable ({exc}) — run `make up`")


@pytest.fixture(autouse=True)
def reset_agent_singleton():
    _require_postgres()
    agent_module._graph = None
    agent_module._checkpointer_cm = None
    yield
    agent_module._graph = None
    agent_module._checkpointer_cm = None


def _fake_llm():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    return GenericFakeChatModel(
        messages=iter([AIMessage(content="A sufficiently long final answer.")])
    )


class TestInitGraphSync:
    def test_builds_a_working_graph(self):
        agent_module.init_graph_sync()
        graph = agent_module.get_graph()
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        # The singleton graph binds the real ChatOpenAI client — just prove
        # it's a real compiled graph with a durable (non-Memory) checkpointer
        # wired in, rather than driving a live LLM call here.
        from langgraph.checkpoint.memory import MemorySaver

        assert not isinstance(graph.checkpointer, MemorySaver)

    def test_second_call_is_a_noop_reusing_the_singleton(self):
        agent_module.init_graph_sync()
        first = agent_module.get_graph()
        agent_module.init_graph_sync()
        assert agent_module.get_graph() is first

    def test_get_graph_falls_back_to_sync_init(self):
        assert agent_module._graph is None
        graph = agent_module.get_graph()
        assert graph is not None
        assert agent_module._graph is graph

    def test_checkpointer_open_failure_propagates_and_leaves_singleton_unset(
        self, monkeypatch
    ):
        """Regression test: the background thread must not fall through to
        loop.run_forever() on a failed checkpointer open — that would leave
        an orphaned thread running forever for a loop nothing will ever use
        (a real bug caught while writing this)."""

        async def failing_open():
            raise RuntimeError("disk full")

        monkeypatch.setattr(agent_module, "_open_checkpointer", failing_open)

        with pytest.raises(RuntimeError, match="disk full"):
            agent_module.init_graph_sync()

        assert agent_module._graph is None


class TestInitGraphAsync:
    def test_builds_a_working_graph(self):
        async def _check():
            graph = await agent_module.init_graph_async()
            from langgraph.checkpoint.memory import MemorySaver

            assert not isinstance(graph.checkpointer, MemorySaver)

        asyncio.run(_check())

    def test_second_call_is_a_noop_reusing_the_singleton(self):
        async def _check():
            first = await agent_module.init_graph_async()
            second = await agent_module.init_graph_async()
            assert second is first

        asyncio.run(_check())


class TestDurabilityAcrossRestart:
    """The whole point of swapping MemorySaver for AsyncPostgresSaver: state
    written by one checkpointer instance must be readable by a completely
    separate instance pointed at the same database — simulating a process
    restart without actually restarting a process.

    Deliberately bypasses app.agent's singleton/threading machinery (that's
    what TestInitGraphSync/TestInitGraphAsync above already prove works) and
    talks to build_graph()+AsyncPostgresSaver directly with a fake LLM, each
    "instance" on its own short-lived asyncio.run() loop with its own fresh
    connection — the cleanest way to actually simulate two separate
    processes sharing one database (exactly the API-process-plus-workers
    shape this migration exists for). Isolated from every other test in
    this file by a unique thread_id, not a unique database — see this
    module's docstring for why that's sufficient here.
    """

    def test_checkpoint_survives_a_fresh_instance_at_the_same_database(self):
        thread_id = str(uuid.uuid4())

        async def _write():
            async with AsyncPostgresSaver.from_conn_string(
                CHECKPOINTER_DATABASE_URL
            ) as saver:
                await saver.setup()
                graph = build_graph(GraphDeps(llm=_fake_llm()), checkpointer=saver)
                await graph.ainvoke(
                    {"messages": [HumanMessage(content="remember this")]},
                    config={"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}},
                )

        async def _read():
            async with AsyncPostgresSaver.from_conn_string(
                CHECKPOINTER_DATABASE_URL
            ) as saver:
                graph = build_graph(GraphDeps(llm=_fake_llm()), checkpointer=saver)
                state = await graph.aget_state(
                    {"configurable": {"thread_id": thread_id}}
                )
                return [m.content for m in state.values["messages"]]

        asyncio.run(_write())
        contents = asyncio.run(_read())
        assert "remember this" in contents


class TestAsyncSeeding:
    """Regression test for a real bug found while manually verifying the
    web UI against a live `uvicorn` process: `_ensure_seeded` (the sync
    `graph.update_state`) was being called from `astream_events_turn` —
    which runs on the SAME event loop `init_graph_async()` opened the
    checkpointer on (originally `AsyncSqliteSaver`, now `AsyncPostgresSaver`
    — the same loop-binding constraint holds for both) — and raised
    `asyncio.InvalidStateError` on the very first turn of any brand-new
    `thread_id` (the checkpointer refuses a sync call from its own loop;
    only a DIFFERENT thread's sync
    call is the supported cross-thread path — see app/agent.py's module
    docstring). Fixed by splitting into `_ensure_seeded` (sync callers:
    `stream_turn`/`answer`) and `_ensure_seeded_async` (async callers:
    `astream_events_turn`/`astream_events_turn_ctx`, using
    `graph.aupdate_state`)."""

    def test_astream_events_turn_seeds_a_brand_new_thread_without_raising(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            agent_module,
            "build_graph",
            lambda checkpointer=None: build_graph(
                GraphDeps(llm=_fake_llm()), checkpointer=checkpointer
            ),
        )

        async def _run():
            await agent_module.init_graph_async()
            events = []
            async for event in agent_module.astream_events_turn(
                "hello", str(uuid.uuid4()), TEST_CTX
            ):
                events.append(event)
            return events

        events = asyncio.run(_run())

        assert not any(e["type"] == "error" for e in events)
        assert events[-1]["type"] == "done"

    def test_astream_events_resume_checks_resumability_without_raising(
        self, monkeypatch
    ):
        """Same bug class, different call site: astream_events_resume
        used the SYNC `resumability_error` (app/graph.py), which also
        raises InvalidStateError from the checkpointer's own loop. Fixed
        by resumability_error_async (aget_state) — this is the real
        production resume path (`make chat-stream`'s HITL round trip),
        so it gets its own regression test rather than relying on
        coverage from the seeding test above.

        Drives the pause via a plain `graph.ainvoke(...)`, not
        `astream_events_turn` — same reasoning as
        `test_cancel_run_ends_a_paused_run_against_a_real_checkpointer`
        below: `graph.astream_events()` raises "No generations found in
        stream" for a `.bind_tools()`-wrapped `GenericFakeChatModel`'s
        tool-calling response (a pre-existing fake-LLM/streaming fixture
        limitation, unrelated to what's under test here). Confirmed
        directly while adding TestResumabilityErrorRejectsAnActivelyRunningThread's
        stricter check above: using `astream_events_turn` here left the
        checkpoint with `state.next=('agent',)` and a task-level `.error`
        set (the agent node had genuinely failed, not paused) rather than
        a real interrupt — `state.tasks[i].interrupts` was empty. Before
        that stricter check existed, `resumability_error_async` didn't
        notice the difference and let the resume proceed into an already
        broken, non-interrupted checkpoint anyway; the stricter check
        correctly rejects it instead — this test's OWN premise (a genuine
        pause) needed fixing, not the stricter check. `ainvoke` reaches
        the exact same paused-at-human_approval checkpoint without
        tripping the fake-LLM/streaming issue at all.
        """
        from langchain_core.messages import AIMessage

        def _mutating_tool_call_llm():
            from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

            return GenericFakeChatModel(
                messages=iter(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "add_note",
                                    "args": {
                                        "title": "T",
                                        "content": "C",
                                        "topic": "company",
                                    },
                                    "id": "c1",
                                }
                            ],
                        ),
                        AIMessage(content="Okay, I saved that note for you."),
                    ]
                )
            )

        monkeypatch.setattr(
            agent_module,
            "build_graph",
            lambda checkpointer=None: build_graph(
                GraphDeps(llm=_mutating_tool_call_llm()), checkpointer=checkpointer
            ),
        )

        async def _run():
            graph = await agent_module.init_graph_async()
            thread_id = str(uuid.uuid4())
            # Drive the turn to a genuine pause at the mandatory
            # human_approval gate — see this test's docstring for why
            # this is ainvoke, not astream_events_turn.
            await graph.ainvoke(
                {"messages": [HumanMessage(content="remember our refund policy")]},
                config={"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}},
            )
            # Now resume it — this is where resumability_error_async's
            # aget_state must be used, not the sync version.
            events = []
            async for event in agent_module.astream_events_resume(
                thread_id, True, TEST_CTX
            ):
                events.append(event)
            return events

        events = asyncio.run(_run())

        assert not any(e["type"] == "error" for e in events)
        assert events[-1]["type"] == "done"

    def test_cancel_run_ends_a_paused_run_against_a_real_checkpointer(self, monkeypatch):
        """cancel_run (GRAPH_PATTERNS.md pattern 36) is a third resume path
        alongside astream_events_resume — same aget_state/ainvoke
        constraint applies, so it gets the same real-AsyncPostgresSaver,
        single-shared-event-loop regression coverage as the other two.

        Drives the pause via a plain `graph.ainvoke(...)`, not
        `astream_events_turn` — verified empirically that
        `graph.astream_events()` raises "No generations found in stream"
        for a `.bind_tools()`-wrapped `GenericFakeChatModel` specifically
        (LangChain's fake model doesn't implement true token streaming for
        tool-calling responses), a pre-existing test-fixture limitation
        unrelated to cancel_run itself — `ainvoke` reaches the exact same
        paused-at-human_approval checkpoint without tripping it.
        """
        from langchain_core.messages import AIMessage, HumanMessage

        def _mutating_tool_call_llm():
            from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

            return GenericFakeChatModel(
                messages=iter(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "add_note",
                                    "args": {"title": "T", "content": "C", "topic": "company"},
                                    "id": "c1",
                                }
                            ],
                        ),
                    ]
                )
            )

        monkeypatch.setattr(
            agent_module,
            "build_graph",
            lambda checkpointer=None: build_graph(
                GraphDeps(llm=_mutating_tool_call_llm()), checkpointer=checkpointer
            ),
        )

        async def _run():
            graph = await agent_module.init_graph_async()
            thread_id = str(uuid.uuid4())
            cfg = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}

            await graph.ainvoke(
                {"messages": [HumanMessage(content="remember our refund policy")]},
                config=cfg,
            )
            paused_state = await graph.aget_state(cfg)
            assert paused_state.next, "should be paused"

            cancelled = await agent_module.cancel_run(thread_id, TEST_CTX)

            final_state = await graph.aget_state(cfg)
            return cancelled, final_state

        cancelled, state = asyncio.run(_run())

        assert cancelled is True
        assert not state.next  # finished, not paused
        assert "Cancelled" in state.values["messages"][-1].content

    def test_cancel_run_returns_false_when_nothing_is_paused(self):
        async def _run():
            await agent_module.init_graph_async()
            return await agent_module.cancel_run(str(uuid.uuid4()), TEST_CTX)

        assert asyncio.run(_run()) is False


class TestResumabilityError:
    def _paused_graph_and_config(self, monkeypatch):
        """A graph paused at human_approval (require_approval=True, a
        tool call pending) — the real "waiting for a human" state
        resumability_error is meant to guard."""
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "calculator",
                                "args": {"expression": "1+1"},
                                "id": "c1",
                            }
                        ],
                    )
                ]
            )
        )
        graph = build_graph(GraphDeps(llm=llm))
        config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
        graph.invoke(
            {"messages": [HumanMessage(content="hi")], "require_approval": True},
            config=config,
        )
        return graph, config

    def test_no_paused_run_is_checkpoint_lost(self):
        graph = build_graph(GraphDeps(llm=_fake_llm()))
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        before = _count(metrics.agent_checkpoint_issue_total, reason="checkpoint_lost")

        # Never invoked — nothing paused, nothing exists for this thread.
        error = resumability_error(graph, config)

        assert error is not None
        assert "checkpoint_lost" in error
        assert (
            _count(metrics.agent_checkpoint_issue_total, reason="checkpoint_lost")
            == before + 1
        )

    def test_matching_schema_version_is_resumable(self, monkeypatch):
        graph, config = self._paused_graph_and_config(monkeypatch)
        assert resumability_error(graph, config) is None

    def test_mismatched_schema_version_is_checkpoint_incompatible(self, monkeypatch):
        graph, config = self._paused_graph_and_config(monkeypatch)
        monkeypatch.setattr("app.graph.STATE_SCHEMA_VERSION", STATE_SCHEMA_VERSION + 1)
        before = _count(
            metrics.agent_checkpoint_issue_total, reason="checkpoint_incompatible"
        )

        error = resumability_error(graph, config)

        assert error is not None
        assert "checkpoint_incompatible" in error
        assert (
            _count(
                metrics.agent_checkpoint_issue_total, reason="checkpoint_incompatible"
            )
            == before + 1
        )

    def test_differing_graph_version_alone_is_not_an_error(self, monkeypatch):
        """graph_version (build SHA) is recorded but never compared — only
        state_schema_version gates resumability (see resumability_error's
        docstring: ordinary deploys change the SHA constantly)."""
        graph, config = self._paused_graph_and_config(monkeypatch)
        state = graph.get_state(config)
        assert state.values.get("graph_version")  # was stamped
        assert resumability_error(graph, config) is None


class TestResumabilityErrorRejectsAnActivelyRunningThread:
    """Regression test for a real race condition found while building
    GRAPH_PATTERNS.md pattern 43's POST /chat/cancel and /chat/resume:
    `state.next` alone is truthy for ANY checkpoint written mid-run,
    between two ordinary supersteps of a turn that's simply still
    executing — not just for a turn genuinely suspended at
    human_approval's interrupt(). Before this fix, `resumability_error`
    only checked `state.next`, so a `POST /chat/cancel`/`/chat/resume`
    that raced an ACTIVELY STREAMING (not yet paused) turn for the same
    thread_id would sail through as "safe to resume" and then start a
    SECOND, competing Pregel execution against the same checkpoint —
    reproduced directly: the racing call silently drove the turn to
    completion via its own `ainvoke`, while the ORIGINAL caller's own
    `astream_events()` received zero further tokens and nothing ever
    raised to surface the problem. The fix requires an ACTUAL pending
    interrupt (`state.tasks[i].interrupts`), not just any pending task —
    the same signal `human_approval`'s own `interrupt()` leaves behind
    and that `_run_graph_stream` already reads to build the
    `approval_required` event."""

    class _SlowFakeLLM(GenericFakeChatModel):
        """A real `await asyncio.sleep(...)` between streamed tokens, so
        the turn genuinely yields control back to the event loop mid-run
        — giving a concurrently-scheduled task a real window to observe
        an in-progress (not paused) checkpoint, the same way two separate
        OS processes racing over shared Postgres state would."""

        async def _astream(self, *args, **kwargs):
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
                await asyncio.sleep(0.05)

    def _install_slow_fake_graph(self, monkeypatch):
        # Same pattern TestAsyncSeeding above uses: monkeypatch
        # agent_module.build_graph so agent_module.init_graph_async()
        # builds against the REAL (Postgres-backed) singleton checkpointer
        # but with a fake LLM — this test needs the real production call
        # chain (astream_events_turn/cancel_run, both reaching into the
        # SAME module-singleton graph via init_graph_async()) for the race
        # to actually reproduce; a locally-built, separate graph object
        # wouldn't share state with what cancel_run() itself looks up.
        # cache_get/cache_set are stubbed out — the default GraphDeps()
        # would hit the real Redis-backed semantic cache
        # (app/semantic_cache.py), irrelevant to what's under test here.
        llm = self._SlowFakeLLM(messages=iter([AIMessage(content="one two three four five")]))
        monkeypatch.setattr(
            agent_module,
            "build_graph",
            lambda checkpointer=None: build_graph(
                GraphDeps(
                    llm=llm,
                    cache_get=lambda ctx, query: None,
                    cache_set=lambda ctx, query, answer, citations: None,
                ),
                checkpointer=checkpointer,
            ),
        )

    def test_mid_run_checkpoint_is_rejected_not_treated_as_paused(self, monkeypatch):
        self._install_slow_fake_graph(monkeypatch)
        thread_id = str(uuid.uuid4())

        async def _run():
            await agent_module.init_graph_async()

            async def drive_turn():
                async for _ in agent_module.astream_events_turn("say something slowly", thread_id, TEST_CTX):
                    pass

            async def snoop_mid_flight():
                await asyncio.sleep(0.02)  # land squarely mid-run, before any pause
                graph = await agent_module.init_graph_async()
                return await resumability_error_async(
                    graph, {"configurable": {"thread_id": thread_id}}
                )

            _, error = await asyncio.gather(drive_turn(), snoop_mid_flight())
            return error

        error = asyncio.run(_run())

        assert error is not None
        assert "checkpoint_lost" in error

    def test_racing_cancel_run_against_a_mid_run_thread_does_not_hijack_it(self, monkeypatch):
        """End-to-end through the real public functions a racing
        POST /chat/cancel (worker's "cancel" job) and the ORIGINAL
        POST /chat/stream/queued turn (worker's "turn" job) would each
        actually call — not just the lower-level resumability check
        above. Reproduced directly against a real live stack before this
        fix: `POST /chat/cancel` fired while a turn was actively
        streaming (not paused) reported success AND the original SSE
        connection went silent (zero further tokens) — this is that
        exact scenario, hermetically."""
        self._install_slow_fake_graph(monkeypatch)
        thread_id = str(uuid.uuid4())

        async def _run():
            tokens = []

            async def drive_turn():
                async for event in agent_module.astream_events_turn(
                    "say something slowly", thread_id, TEST_CTX
                ):
                    if event["type"] == "token":
                        tokens.append(event["content"])

            async def race_cancel():
                await asyncio.sleep(0.02)
                return await agent_module.cancel_run(thread_id, TEST_CTX)

            _, cancelled = await asyncio.gather(drive_turn(), race_cancel())
            return tokens, cancelled

        tokens, cancelled = asyncio.run(_run())

        # The real bug: before the fix, `cancelled` was True and `tokens`
        # was empty — the racing cancel_run call had silently taken over
        # execution instead of being rejected, starving the original
        # caller's own stream.
        assert cancelled is False
        assert "".join(tokens) == "one two three four five"
