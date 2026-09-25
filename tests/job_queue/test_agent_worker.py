"""Tests for app/job_queue/agent_worker.py's process_request — the Redis Streams
consumer side of GRAPH_PATTERNS.md pattern 43, dispatching by
`payload["kind"]` (`"turn"` | `"resume"` | `"cancel"`). Reuses
tests/job_queue/test_queue.py's FakeRedis; the actual graph-running functions
(`astream_events_turn`/`astream_events_resume`/`cancel_run`) are
monkeypatched so these never touch a real graph/LLM.
"""
import asyncio
import json

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


class TestHandleReclaimedJob:
    """`_handle_reclaimed_job` is what `_reclaim_loop` calls for every entry
    `queue.py::reclaim_stale_entries` finds abandoned by a dead worker —
    see that loop's own docstring for why this surfaces+archives instead of
    ever redelivering the job to run again (a duplicate side-effect risk)."""

    async def test_publishes_a_worker_lost_error_archives_and_acks(self, monkeypatch):
        client = FakeRedis()
        entry_id, fields = _entry(kind="turn", request_id="r1", thread_id="t1")
        before = _count(agent_worker.metrics.agent_worker_job_reclaimed_total, queue="agent")

        await agent_worker._handle_reclaimed_job(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "worker_lost"

        dead = client.streams[dead_letter_stream_key(agent_worker.REQUESTS_STREAM)]
        assert len(dead) == 1
        assert dead[0][1]["original_entry_id"] == entry_id
        assert json.loads(dead[0][1]["payload"])["request_id"] == "r1"

        assert client.acked == [entry_id]
        assert (
            _count(agent_worker.metrics.agent_worker_job_reclaimed_total, queue="agent")
            == before + 1
        )

    async def test_covers_non_turn_kinds_too(self, monkeypatch):
        """The lock a reclaimed job held is scoped by thread_id, not job
        kind — reclaim must handle whichever kind was abandoned, not just
        "turn" (_handle_reclaimed_job never branches on `kind` at all, but
        this guards against a future change that assumes "turn")."""
        client = FakeRedis()
        entry_id, fields = _entry(kind="resume", request_id="r2", thread_id="t2")

        await agent_worker._handle_reclaimed_job(client, entry_id, fields)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert events[0]["code"] == "worker_lost"
        assert client.acked == [entry_id]

    async def test_an_unreadable_payload_still_acks_and_dead_letters_without_raising(self):
        client = FakeRedis()
        entry_id = "9-0"
        fields = {"payload": "not valid json"}

        await agent_worker._handle_reclaimed_job(client, entry_id, fields)  # must not raise

        assert client.acked == [entry_id]
        dead = client.streams[dead_letter_stream_key(agent_worker.REQUESTS_STREAM)]
        assert len(dead) == 1


class TestReclaimLoop:
    async def test_reclaims_an_abandoned_entry_then_stops_when_signalled(self, monkeypatch):
        monkeypatch.setattr(agent_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r5", thread_id="t5")
        client.streams[agent_worker.REQUESTS_STREAM] = [(entry_id, fields)]
        client._delivered[entry_id] = 999_999_999  # already long abandoned

        stop_event = asyncio.Event()
        task = asyncio.create_task(agent_worker._reclaim_loop(client, stop_event))
        await asyncio.sleep(0.05)  # let at least one pass run
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r5")]]
        assert events[0]["code"] == "worker_lost"
        assert entry_id in client.acked

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
        task = asyncio.create_task(agent_worker._reclaim_loop(client, stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        assert len(calls) >= 2  # survived the first pass's failure and ran again


async def _noop_async(*args, **kwargs):
    return None
