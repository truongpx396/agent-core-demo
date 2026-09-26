"""Tests for app/job_queue/agent_worker.py's process_request — the Redis Streams
consumer side of GRAPH_PATTERNS.md pattern 43, dispatching by
`payload["kind"]` (`"turn"` | `"resume"` | `"cancel"`). Reuses
tests/job_queue/test_queue.py's FakeRedis; the actual graph-running functions
(`astream_events_turn`/`astream_events_resume`/`cancel_run`) are
monkeypatched so these never touch a real graph/LLM.
"""
import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.job_queue import agent_worker
from app.job_queue.queue import (
    CONSUMER_GROUP,
    dead_letter_stream_key,
    results_stream_key,
)
from tests.conftest import metric_value as _count
from tests.job_queue.test_queue import FakeRedis

REQUESTS_STREAM = agent_worker.REQUESTS_STREAM  # this test module's worker
# runs as the default AGENT_DOMAIN ("ecorp"); see app/job_queue/agent_worker.py's
# own docstring on why the requests stream is a per-process, not per-message,
# property.


def _entry(
    kind="turn",
    request_id="r1",
    text="hi",
    thread_id="t1",
    ctx=None,
    require_approval=False,
    images=None,
    approved=True,
):
    ctx = ctx or {"tenant": "ecorp", "principal": "p1", "claims": {}}
    if kind == "turn":
        payload = {
            "kind": "turn",
            "request_id": request_id,
            "text": text,
            "thread_id": thread_id,
            "ctx": ctx,
            "require_approval": require_approval,
            "images": images or [],
        }
    elif kind == "resume":
        payload = {
            "kind": "resume",
            "request_id": request_id,
            "thread_id": thread_id,
            "approved": approved,
            "ctx": ctx,
        }
    elif kind == "cancel":
        payload = {"kind": "cancel", "request_id": request_id, "thread_id": thread_id, "ctx": ctx}
    else:
        payload = {"kind": kind, "request_id": request_id, "thread_id": thread_id, "ctx": ctx}
    return "1-0", {"payload": json.dumps(payload)}


class TestProcessRequestTurn:
    async def test_publishes_every_yielded_event_and_acks(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "token", "content": "Hel"}
            yield {"type": "token", "content": "lo"}
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r1")

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert events == [
            {"type": "token", "content": "Hel"},
            {"type": "token", "content": "lo"},
            {"type": "done"},
        ]
        assert client.acked == [entry_id]

    async def test_a_missing_kind_field_defaults_to_turn(self, monkeypatch):
        """Backward compatibility: any payload published before `kind`
        existed (or any future producer that forgets it) is still a
        `"turn"` job, not a dispatch error."""
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r1")
        del fields["payload"]
        fields["payload"] = json.dumps(
            {
                "request_id": "r1",
                "text": "hi",
                "thread_id": "t1",
                "ctx": {"tenant": "ecorp", "principal": "p1", "claims": {}},
                "require_approval": False,
                "images": [],
            }
        )

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert events == [{"type": "done"}]

    async def test_clears_any_stale_cancel_flag_before_starting(self, monkeypatch):
        """A flag left over from a PRIOR turn on this thread_id (e.g.
        /chat/cancel raced with that turn already finishing on its own)
        must not spuriously cancel this brand-new one. Captures the
        cancel_check RESULT into a plain list rather than asserting inside
        the fake generator — an assertion failure in there would just be
        caught by process_request's own try/except and published as an
        ordinary error event instead of failing this test."""
        from app.job_queue.queue import is_cancelled, set_cancel_flag

        seen_cancelled = []

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            seen_cancelled.append(await cancel_check())
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r1", thread_id="t1")

        async def _run():
            await set_cancel_flag(client, "t1")
            await agent_worker.process_request(client, entry_id, fields)
            return await is_cancelled(client, "t1")

        # Also confirm the flag reads back False directly, independent of
        # what the fake turn observed.
        still_cancelled_after = await _run()
        assert seen_cancelled == [False]
        assert still_cancelled_after is False

    async def test_passes_a_working_cancel_check_bound_to_the_thread_id(self, monkeypatch):
        from app.job_queue.queue import set_cancel_flag

        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured["cancel_check"] = cancel_check
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r1", thread_id="t1")

        async def _run():
            await agent_worker.process_request(client, entry_id, fields)
            # Set the flag AFTER the turn "finished" (fake) — proves the
            # captured cancel_check reads live state, not a snapshot.
            await set_cancel_flag(client, "t1")
            return await captured["cancel_check"]()

        assert await _run() is True

    async def test_passes_the_decoded_payload_fields_through(self, monkeypatch):
        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured.update(text=text, thread_id=thread_id, ctx=ctx, require_approval=require_approval)
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        ctx = {"tenant": "ecorp", "principal": "p9", "claims": {}}
        entry_id, fields = _entry(text="what is 2+2?", thread_id="t9", ctx=ctx, require_approval=True)

        await agent_worker.process_request(client, entry_id, fields)

        assert captured == {
            "text": "what is 2+2?",
            "thread_id": "t9",
            "ctx": ctx,
            "require_approval": True,
        }

    async def test_passes_images_through_when_attached(self, monkeypatch):
        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured["images"] = images
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r4", images=["https://example.com/cat.png"])

        await agent_worker.process_request(client, entry_id, fields)

        assert captured["images"] == ["https://example.com/cat.png"]

    async def test_no_images_attached_passes_none_not_an_empty_list(self, monkeypatch):
        """_build_human_content (app/agent/runtime_stream.py) treats an empty list the
        same as None, but keeping the distinction here means a future
        reader can tell "no image was ever attached" from "an empty list
        was explicitly sent" by reading the call, not by re-deriving it."""
        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured["images"] = images
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r5")

        await agent_worker.process_request(client, entry_id, fields)

        assert captured["images"] is None

    async def test_a_failure_publishes_an_error_event_and_still_acks(self, monkeypatch):
        async def failing_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            raise RuntimeError("graph blew up")
            yield  # pragma: no cover - unreachable, makes this a generator

        monkeypatch.setattr(agent_worker, "astream_events_turn", failing_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r2")

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert events == [{"type": "error", "content": "graph blew up"}]
        assert client.acked == [entry_id]


class TestProcessRequestResume:
    async def test_dispatches_to_astream_events_resume_with_the_right_args(self, monkeypatch):
        captured = {}

        async def fake_resume(thread_id, approved, ctx):
            captured.update(thread_id=thread_id, approved=approved, ctx=ctx)
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_resume", fake_resume)
        client = FakeRedis()
        ctx = {"tenant": "ecorp", "principal": "p1", "claims": {}}
        entry_id, fields = _entry(kind="resume", request_id="r6", thread_id="t6", ctx=ctx, approved=False)

        await agent_worker.process_request(client, entry_id, fields)

        assert captured == {"thread_id": "t6", "approved": False, "ctx": ctx}
        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r6")]]
        assert events == [{"type": "done"}]
        assert client.acked == [entry_id]

    async def test_a_resume_failure_publishes_an_error_and_still_acks(self, monkeypatch):
        async def failing_resume(thread_id, approved, ctx):
            raise RuntimeError("checkpoint gone")
            yield  # pragma: no cover

        monkeypatch.setattr(agent_worker, "astream_events_resume", failing_resume)
        client = FakeRedis()
        entry_id, fields = _entry(kind="resume", request_id="r7")

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r7")]]
        assert events == [{"type": "error", "content": "checkpoint gone"}]
        assert client.acked == [entry_id]


class TestProcessRequestCancel:
    async def test_a_successful_cancel_publishes_a_cancelled_error_event(self, monkeypatch):
        captured = {}

        async def fake_cancel_run(thread_id, ctx):
            captured.update(thread_id=thread_id, ctx=ctx)
            return True

        monkeypatch.setattr(agent_worker, "cancel_run", fake_cancel_run)
        client = FakeRedis()
        ctx = {"tenant": "ecorp", "principal": "p1", "claims": {}}
        entry_id, fields = _entry(kind="cancel", request_id="r8", thread_id="t8", ctx=ctx)

        await agent_worker.process_request(client, entry_id, fields)

        assert captured == {"thread_id": "t8", "ctx": ctx}
        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r8")]]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "cancelled"
        assert client.acked == [entry_id]

    async def test_nothing_to_cancel_publishes_a_plain_done(self, monkeypatch):
        """cancel_run returns False when nothing was paused (e.g. the
        thread is actively streaming instead, handled by the separate
        cancel-flag mechanism, not this job) — reported as a quiet "done,"
        not an error, since nothing actually went wrong."""

        async def fake_cancel_run(thread_id, ctx):
            return False

        monkeypatch.setattr(agent_worker, "cancel_run", fake_cancel_run)
        client = FakeRedis()
        entry_id, fields = _entry(kind="cancel", request_id="r9")

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r9")]]
        assert events == [{"type": "done"}]
        assert client.acked == [entry_id]


class TestProcessRequestUnknownKind:
    async def test_unknown_kind_publishes_an_error_and_still_acks(self):
        """A malformed/future-version payload must not silently hang or
        crash the worker loop — same "always ack, publish an error"
        contract as any other processing failure."""
        client = FakeRedis()
        entry_id, fields = _entry(kind="not-a-real-kind", request_id="r10")

        await agent_worker.process_request(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r10")]]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "not-a-real-kind" in events[0]["content"]
        assert client.acked == [entry_id]


class TestRunLoop:
    async def test_processes_one_request_end_to_end_via_the_consumer_group(self, monkeypatch):
        """A thin proof that run()'s xreadgroup wiring actually delivers a
        published request to process_request — not a re-test of
        FakeRedis's own semantics (see tests/job_queue/test_queue.py for those)."""

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        monkeypatch.setattr(agent_worker, "init_graph_async", _noop_async)
        client = FakeRedis()
        monkeypatch.setattr(agent_worker, "get_client", lambda: client)

        async def _run_one_iteration():
            from app.job_queue.queue import ensure_consumer_group

            await ensure_consumer_group(client)
            entry_id, fields = _entry(request_id="r3")
            client.streams[REQUESTS_STREAM].append((entry_id, fields))
            response = await client.xreadgroup(
                CONSUMER_GROUP, agent_worker.CONSUMER_NAME, {REQUESTS_STREAM: ">"}, count=1
            )
            _, entries = response[0]
            for eid, f in entries:
                await agent_worker.process_request(client, eid, f)

        await _run_one_iteration()

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r3")]]
        assert events == [{"type": "done"}]


class TestConcurrentDispatch:
    """run() no longer awaits process_request one job at a time — it
    acquires a semaphore slot, then asyncio.create_tasks _process_with_limit
    per entry (see run()'s own comments for why the semaphore is acquired
    BEFORE task creation, not inside it). This replicates that exact
    acquire-then-dispatch pattern against several jobs at once — same
    "thin proof of the real wiring" spirit as TestRunLoop above, extended to
    the concurrency behavior specifically."""

    async def test_bounds_concurrency_and_actually_overlaps(self, monkeypatch):
        max_concurrency = 2
        num_jobs = 5
        current = 0
        peak = 0
        count_lock = asyncio.Lock()

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            nonlocal current, peak
            async with count_lock:
                current += 1
                peak = max(peak, current)
            await asyncio.sleep(0.05)  # long enough for siblings to overlap, short enough for a fast test
            async with count_lock:
                current -= 1
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _dispatch_all():
            tasks = []
            for i in range(num_jobs):
                # Distinct thread_ids: these jobs are meant to prove
                # WORKER-level concurrency (the semaphore bound), not
                # same-thread races — process_request's own per-thread
                # lock (TestSameThreadJobsAreSerialized below) would
                # otherwise reject every job but the first here and this
                # test would never reach peak == max_concurrency.
                entry_id, fields = _entry(request_id=f"r{i}", thread_id=f"t{i}")
                await semaphore.acquire()
                task = asyncio.create_task(
                    agent_worker._process_with_limit(client, entry_id, fields, semaphore)
                )
                tasks.append(task)
            await asyncio.gather(*tasks)

        await _dispatch_all()

        # Never exceeded the cap...
        assert peak <= max_concurrency
        # ...but genuinely reached it — proves siblings actually overlapped
        # in wall-clock time rather than running strictly one at a time
        # (which would leave peak == 1 no matter how many jobs ran).
        assert peak == max_concurrency
        assert len(client.acked) == num_jobs
        for i in range(num_jobs):
            events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key(f"r{i}")]]
            assert events == [{"type": "done"}]


class TestSameThreadJobsAreSerialized:
    """process_request's per-thread lock (queue.py::acquire_thread_lock) —
    the fix for the checkpoint-fork race two jobs on the SAME thread_id
    used to be able to hit (see agent_worker.py's own module docstring):
    both would read the checkpointer's latest state as their parent and
    both write a child from it, silently corrupting whichever one lost.
    Distinct from TestConcurrentDispatch above, which proves DIFFERENT
    thread_ids genuinely overlap — this proves the SAME thread_id does
    NOT, by design."""

    async def test_a_second_turn_on_the_same_thread_is_rejected_fast_not_queued(self, monkeypatch):
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()
        calls = []

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            calls.append(1)
            handler_started.set()
            await release_handler.wait()
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id_1, fields_1 = _entry(request_id="r1", thread_id="same-thread")
        entry_id_2, fields_2 = _entry(request_id="r2", thread_id="same-thread")

        first = asyncio.create_task(agent_worker.process_request(client, entry_id_1, fields_1))
        await handler_started.wait()  # first job now genuinely holds the lock
        await agent_worker.process_request(client, entry_id_2, fields_2)  # second, while first is still in flight
        release_handler.set()
        await first

        # The second job never even reached the graph — rejected before
        # calling astream_events_turn at all.
        assert calls == [1]
        r2_events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert len(r2_events) == 1
        assert r2_events[0]["type"] == "error"
        assert r2_events[0]["code"] == "thread_busy"
        # Still acked — a rejected job must not be redelivered either.
        assert entry_id_2 in client.acked
        r1_events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert r1_events == [{"type": "done"}]

    async def test_lock_releases_at_the_terminal_event_not_after_trailing_cleanup(self, monkeypatch):
        """Regression guard for the real bug found in
        tests/integration/test_worker_scaling.py's real-subprocess HITL
        test: the FIRST version of this fix released the lock in
        process_request's own outer `finally` (after `handler()` fully
        returned), which still left a window between "terminal event
        published, client can already act on it" and "lock actually
        freed" — wide enough that ~1/3 of a real 40-way concurrent
        pause/resume load got spuriously rejected as THREAD_BUSY. The fix
        releases the lock the INSTANT the handler publishes a terminal
        event, before the underlying astream_events_turn generator's own
        trailing cleanup (Langfuse trace close, budget-release bookkeeping)
        even runs. Proven here by making that trailing cleanup slow and
        observable: a second job on the same thread must succeed WHILE
        it's still in progress, not after."""
        trailing_cleanup_started = asyncio.Event()
        release_trailing_cleanup = asyncio.Event()
        calls = 0

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            # Only the FIRST call (r1) does the slow trailing-cleanup
            # dance — r2 reuses this same monkeypatched function (both
            # jobs target the same thread_id), and must complete
            # immediately so this test can prove IT succeeded while r1's
            # own cleanup was still in progress, not get dragged into it.
            nonlocal calls
            calls += 1
            is_first_call = calls == 1
            yield {"type": "done"}
            if is_first_call:
                # Simulates astream_events_turn's own post-yield cleanup
                # (runtime_stream.py's fire-and-forget budget-release task
                # creation, closing the Langfuse trace) — genuinely slow
                # here so a premature-release regression would show up
                # directly.
                trailing_cleanup_started.set()
                await release_trailing_cleanup.wait()

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id_1, fields_1 = _entry(request_id="r1", thread_id="same-thread")
        entry_id_2, fields_2 = _entry(request_id="r2", thread_id="same-thread")

        first = asyncio.create_task(agent_worker.process_request(client, entry_id_1, fields_1))
        await trailing_cleanup_started.wait()  # "done" already published; first job's own cleanup still running
        await agent_worker.process_request(client, entry_id_2, fields_2)
        release_trailing_cleanup.set()
        await first

        r2_events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert r2_events == [{"type": "done"}], (
            "the second job was rejected while the first was still in its own "
            "trailing cleanup — the lock was released too late"
        )

    async def test_a_turn_and_a_resume_on_the_same_thread_also_exclude_each_other(self, monkeypatch):
        """The lock is keyed by thread_id, not by job kind — a resume
        racing a fresh turn on the same thread_id is exactly the "double
        texting onto a paused thread" scenario runtime_stream.py's own
        PENDING_APPROVAL handling already guards at the graph level; this
        proves the dispatcher-level lock backs it up too."""
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            handler_started.set()
            await release_handler.wait()
            yield {"type": "done"}

        async def fake_resume(thread_id, approved, ctx):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        monkeypatch.setattr(agent_worker, "astream_events_resume", fake_resume)
        client = FakeRedis()
        entry_id_1, fields_1 = _entry(kind="turn", request_id="r1", thread_id="same-thread")
        entry_id_2, fields_2 = _entry(kind="resume", request_id="r2", thread_id="same-thread")

        turn_task = asyncio.create_task(agent_worker.process_request(client, entry_id_1, fields_1))
        await handler_started.wait()
        await agent_worker.process_request(client, entry_id_2, fields_2)
        release_handler.set()
        await turn_task

        r2_events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert r2_events[0]["code"] == "thread_busy"

    async def test_a_third_job_proceeds_normally_once_the_lock_is_released(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id_1, fields_1 = _entry(request_id="r1", thread_id="same-thread")
        entry_id_2, fields_2 = _entry(request_id="r2", thread_id="same-thread")

        await agent_worker.process_request(client, entry_id_1, fields_1)  # runs and releases
        await agent_worker.process_request(client, entry_id_2, fields_2)  # lock is free again

        r2_events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert r2_events == [{"type": "done"}]

    async def test_different_thread_ids_never_contend(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id_1, fields_1 = _entry(request_id="r1", thread_id="thread-a")
        entry_id_2, fields_2 = _entry(request_id="r2", thread_id="thread-b")

        await asyncio.gather(
            agent_worker.process_request(client, entry_id_1, fields_1),
            agent_worker.process_request(client, entry_id_2, fields_2),
        )

        for rid in ("r1", "r2"):
            events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key(rid)]]
            assert events == [{"type": "done"}]


class FakeGraph:
    """Stands in for the real compiled graph's `aget_state` — just enough
    to drive `_is_safe_to_retry_turn` without a real checkpointer/Postgres.
    `states` maps thread_id -> the message list that thread's checkpoint
    would report; a thread with no entry behaves like one that was never
    checkpointed at all (empty `.values`)."""

    def __init__(self, states: dict | None = None):
        self._states = states or {}

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return SimpleNamespace(values={"messages": self._states.get(thread_id, [])})


class RaisingGraph:
    async def aget_state(self, config):
        raise RuntimeError("checkpoint deserialization failed")


class TestIsSafeToRetryTurn:
    """`_is_safe_to_retry_turn` is the safety check standing between "a
    crashed worker's turn" and "silently running it again" — see this
    module's own docstring for why only `mutating`/`outward` tool calls
    (never `read_only` ones) make that unsafe."""

    async def test_no_checkpoint_at_all_is_safe(self):
        graph = FakeGraph()  # thread never seen -> empty state
        assert await agent_worker._is_safe_to_retry_turn(graph, {}, "t1") is True

    async def test_a_human_message_with_nothing_after_it_is_safe(self):
        graph = FakeGraph({"t1": [HumanMessage(content="hi")]})
        assert await agent_worker._is_safe_to_retry_turn(graph, {}, "t1") is True

    async def test_a_completed_read_only_tool_call_is_safe(self):
        graph = FakeGraph(
            {"t1": [HumanMessage(content="hi"), ToolMessage(content="42", name="calculator", tool_call_id="c1")]}
        )
        assert (
            await agent_worker._is_safe_to_retry_turn(graph, {"calculator": "read_only"}, "t1")
            is True
        )

    async def test_a_completed_mutating_tool_call_is_not_safe(self):
        graph = FakeGraph(
            {"t1": [HumanMessage(content="add a note"), ToolMessage(content="ok", name="add_note", tool_call_id="c1")]}
        )
        assert (
            await agent_worker._is_safe_to_retry_turn(graph, {"add_note": "mutating"}, "t1")
            is False
        )

    async def test_a_tool_missing_from_capabilities_fails_closed_as_outward(self):
        """Same default `graph_routing.py`'s own gate uses for an
        undeclared tool — never assume unknown means safe."""
        graph = FakeGraph(
            {"t1": [HumanMessage(content="hi"), ToolMessage(content="ok", name="mystery_tool", tool_call_id="c1")]}
        )
        assert await agent_worker._is_safe_to_retry_turn(graph, {}, "t1") is False

    async def test_only_looks_after_the_most_recent_human_message(self):
        """A mutating tool call from an EARLIER, already-completed turn on
        this same thread must not poison the safety check for a later,
        still-fresh turn that hasn't touched any tool yet."""
        graph = FakeGraph(
            {
                "t1": [
                    HumanMessage(content="add a note"),
                    ToolMessage(content="ok", name="add_note", tool_call_id="c1"),
                    AIMessage(content="Done."),
                    HumanMessage(content="what's the weather"),  # this turn's own message
                ]
            }
        )
        assert (
            await agent_worker._is_safe_to_retry_turn(graph, {"add_note": "mutating"}, "t1")
            is True
        )

    async def test_an_unreadable_checkpoint_fails_closed(self):
        assert await agent_worker._is_safe_to_retry_turn(RaisingGraph(), {}, "t1") is False

    async def test_a_turn_that_already_produced_a_final_answer_is_not_safe(self):
        """Closes a real, narrower gap: a plain Q&A turn with NO tool
        calls at all that fully finished (usage already recorded via
        _record_turn_metrics) but crashed before this job's own ack would
        otherwise be judged "safe" by the tool-call check alone and
        blindly re-run — wasting a second LLM call and double-recording
        that turn's usage cost for no benefit."""
        graph = FakeGraph(
            {"t1": [HumanMessage(content="what's 2+2?"), AIMessage(content="4")]}
        )
        assert await agent_worker._is_safe_to_retry_turn(graph, {}, "t1") is False

    async def test_a_turn_still_holding_pending_tool_calls_is_not_yet_completed(self):
        """An AIMessage that itself REQUESTS tool calls (not yet executed
        — no ToolMessage exists for it) means the crash happened before
        the tools even ran, not after the turn finished — must not be
        confused with a genuinely completed final answer."""
        graph = FakeGraph(
            {
                "t1": [
                    HumanMessage(content="add a note about X"),
                    AIMessage(content="", tool_calls=[{"name": "add_note", "args": {}, "id": "c1"}]),
                ]
            }
        )
        assert await agent_worker._is_safe_to_retry_turn(graph, {"add_note": "mutating"}, "t1") is True


class TestHandleReclaimedJob:
    """`_handle_reclaimed_job` is what `_reclaim_loop` calls for every entry
    `queue.py::reclaim_stale_entries` finds abandoned by a dead worker — see
    this module's own docstring for the full per-kind policy. `graph`/
    `tool_capabilities` below default to an empty `FakeGraph()`/`{}` (i.e.
    "nothing ran yet") except where a test needs otherwise."""

    async def test_a_turn_that_never_ran_a_tool_is_silently_retried_not_dead_lettered(self):
        client = FakeRedis()
        entry_id, fields = _entry(kind="turn", request_id="r1", thread_id="t1")
        before_retried = _count(agent_worker.metrics.agent_worker_job_reclaimed_total, queue="agent", outcome="retried")

        await agent_worker._handle_reclaimed_job(client, entry_id, fields, graph=FakeGraph(), tool_capabilities={})

        # No error surfaced, nothing dead-lettered — the caller listening on
        # r1's results stream stays blocked, transparently waiting for the
        # retry's own real outcome instead.
        assert results_stream_key("r1") not in client.streams
        assert dead_letter_stream_key(agent_worker.REQUESTS_STREAM) not in client.streams
        assert client.acked == [entry_id]

        republished = client.streams[agent_worker.REQUESTS_STREAM]
        assert len(republished) == 1
        new_payload = json.loads(republished[0][1]["payload"])
        assert new_payload["request_id"] == "r1"
        assert new_payload["_reclaim_attempts"] == 1
        assert (
            _count(agent_worker.metrics.agent_worker_job_reclaimed_total, queue="agent", outcome="retried")
            == before_retried + 1
        )

    async def test_a_turn_that_already_ran_a_mutating_tool_is_dead_lettered_not_retried(self):
        client = FakeRedis()
        entry_id, fields = _entry(kind="turn", request_id="r2", thread_id="t2")
        graph = FakeGraph(
            {"t2": [HumanMessage(content="hi"), ToolMessage(content="ok", name="add_note", tool_call_id="c1")]}
        )

        await agent_worker._handle_reclaimed_job(
            client, entry_id, fields, graph=graph, tool_capabilities={"add_note": "mutating"}
        )

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert events[0]["code"] == "worker_lost"
        dead = client.streams[dead_letter_stream_key(agent_worker.REQUESTS_STREAM)]
        assert len(dead) == 1
        assert client.acked == [entry_id]
        assert agent_worker.REQUESTS_STREAM not in client.streams or all(
            json.loads(f["payload"]).get("request_id") != "r2"
            for _, f in client.streams.get(agent_worker.REQUESTS_STREAM, [])
        )  # never republished

    async def test_a_turn_already_at_the_retry_cap_is_dead_lettered_even_though_safe(self, monkeypatch):
        """A "poison pill" job that keeps crashing whichever worker picks
        it up must eventually land in the dead letter stream for a human,
        not loop crash/reclaim/retry forever."""
        monkeypatch.setattr(agent_worker, "MAX_AUTO_RECLAIM_RETRIES", 1)
        client = FakeRedis()
        entry_id, fields = _entry(kind="turn", request_id="r3", thread_id="t3")
        payload = json.loads(fields["payload"])
        payload["_reclaim_attempts"] = 1  # already retried once
        fields = {"payload": json.dumps(payload)}

        await agent_worker._handle_reclaimed_job(client, entry_id, fields, graph=FakeGraph(), tool_capabilities={})

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r3")]]
        assert events[0]["code"] == "worker_lost"
        assert len(client.streams[dead_letter_stream_key(agent_worker.REQUESTS_STREAM)]) == 1

    async def test_a_resume_is_always_auto_retried_now_that_tools_are_idempotent(self):
        """Unlike "turn", "resume" needs no checkpoint inspection at all —
        it continues an EXISTING checkpoint's already-pending tool_calls
        under their own original tool_call_ids, and every mutating/outward
        tool now dedupes on that id (app/agent/tool_idempotency.py), so
        re-invoking one that already completed just returns its cached
        result instead of running again. Graph/capabilities are irrelevant
        here on purpose — nothing about this decision reads them."""
        client = FakeRedis()
        entry_id, fields = _entry(kind="resume", request_id="r4", thread_id="t4")

        await agent_worker._handle_reclaimed_job(client, entry_id, fields, graph=FakeGraph(), tool_capabilities={})

        assert results_stream_key("r4") not in client.streams
        assert dead_letter_stream_key(agent_worker.REQUESTS_STREAM) not in client.streams
        assert client.acked == [entry_id]
        republished = client.streams[agent_worker.REQUESTS_STREAM]
        assert json.loads(republished[0][1]["payload"])["kind"] == "resume"

    async def test_a_cancel_is_always_auto_retried(self):
        """Cancelling is inherently idempotent — safe regardless of
        checkpoint state, no inspection needed at all."""
        client = FakeRedis()
        entry_id, fields = _entry(kind="cancel", request_id="r5", thread_id="t5")

        await agent_worker._handle_reclaimed_job(client, entry_id, fields, graph=FakeGraph(), tool_capabilities={})

        assert results_stream_key("r5") not in client.streams
        assert dead_letter_stream_key(agent_worker.REQUESTS_STREAM) not in client.streams
        assert client.acked == [entry_id]
        republished = client.streams[agent_worker.REQUESTS_STREAM]
        assert json.loads(republished[0][1]["payload"])["kind"] == "cancel"

    async def test_an_unreadable_payload_still_acks_and_dead_letters_without_raising(self):
        client = FakeRedis()
        entry_id = "9-0"
        fields = {"payload": "not valid json"}

        await agent_worker._handle_reclaimed_job(
            client, entry_id, fields, graph=FakeGraph(), tool_capabilities={}
        )  # must not raise

        assert client.acked == [entry_id]
        dead = client.streams[dead_letter_stream_key(agent_worker.REQUESTS_STREAM)]
        assert len(dead) == 1


class TestReclaimLoop:
    async def test_reclaims_a_dead_lettered_kind_end_to_end_then_stops_when_signalled(self, monkeypatch):
        """A "turn" that already ran a mutating tool is used here
        specifically because it's the one case still dead-lettered
        unconditionally (every other kind is now retried — see this
        module's own docstring) — proves the loop's own wiring
        (reclaim -> handle -> stop) without needing a separate scenario."""
        monkeypatch.setattr(agent_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client = FakeRedis()
        entry_id, fields = _entry(kind="turn", request_id="r5", thread_id="t5")
        client.streams[agent_worker.REQUESTS_STREAM] = [(entry_id, fields)]
        client._delivered[entry_id] = 999_999_999  # already long abandoned
        graph = FakeGraph(
            {"t5": [HumanMessage(content="hi"), ToolMessage(content="ok", name="add_note", tool_call_id="c1")]}
        )

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            agent_worker._reclaim_loop(
                client, stop_event, graph=graph, tool_capabilities={"add_note": "mutating"}
            )
        )
        await asyncio.sleep(0.05)  # let at least one pass run
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r5")]]
        assert events[0]["code"] == "worker_lost"
        assert entry_id in client.acked

    async def test_reclaims_a_retryable_turn_end_to_end(self, monkeypatch):
        monkeypatch.setattr(agent_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client = FakeRedis()
        _, fields = _entry(kind="turn", request_id="r6", thread_id="t6")
        # Via xadd + xreadgroup (not a direct `client.streams[...] = ...`
        # splice like the sibling test above) so the entry gets a REAL,
        # distinct id from FakeRedis's own counter — needed here because
        # this test's own retry republishes a second entry onto the same
        # stream, and a collision with `entry_id` would make the two
        # indistinguishable.
        entry_id = await client.xadd(agent_worker.REQUESTS_STREAM, fields)
        await client.xreadgroup("some-other-group", "dead-consumer", {agent_worker.REQUESTS_STREAM: ">"})
        client._delivered[entry_id] = 999_999_999

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            agent_worker._reclaim_loop(client, stop_event, graph=FakeGraph(), tool_capabilities={})
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        assert entry_id in client.acked
        assert results_stream_key("r6") not in client.streams  # no error surfaced
        republished = [
            f for eid, f in client.streams[agent_worker.REQUESTS_STREAM] if eid != entry_id
        ]
        assert len(republished) == 1
        assert json.loads(republished[0]["payload"])["request_id"] == "r6"

    async def test_a_redis_error_during_a_pass_does_not_kill_the_loop(self, monkeypatch):
        """One bad reclaim pass (a transient Redis blip) must not end
        reclaim coverage for this whole worker process's life — the next
        interval should still run."""
        monkeypatch.setattr(agent_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client = FakeRedis()
        calls = []

        async def flaky_reclaim(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("redis blip")
            return []

        monkeypatch.setattr(agent_worker, "reclaim_stale_entries", flaky_reclaim)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            agent_worker._reclaim_loop(client, stop_event, graph=FakeGraph(), tool_capabilities={})
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        assert len(calls) >= 2  # survived the first pass's failure and ran again


async def _noop_async(*args, **kwargs):
    return None
