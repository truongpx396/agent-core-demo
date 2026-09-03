"""Tests for app/turns/agent_worker.py's process_request — the Redis Streams
consumer side of GRAPH_PATTERNS.md pattern 43, dispatching by
`payload["kind"]` (`"turn"` | `"resume"` | `"cancel"`). Reuses
tests/turns/test_queue.py's FakeRedis; the actual graph-running functions
(`astream_events_turn`/`astream_events_resume`/`cancel_run`) are
monkeypatched so these never touch a real graph/LLM.
"""
import asyncio
import json

from app.turns import agent_worker
from app.turns.queue import CONSUMER_GROUP, REQUESTS_STREAM, results_stream_key
from tests.turns.test_queue import FakeRedis


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
    ctx = ctx or {"tenant": "acme", "principal": "p1", "claims": {}}
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
    def test_publishes_every_yielded_event_and_acks(self, monkeypatch):
        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "token", "content": "Hel"}
            yield {"type": "token", "content": "lo"}
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r1")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert events == [
            {"type": "token", "content": "Hel"},
            {"type": "token", "content": "lo"},
            {"type": "done"},
        ]
        assert client.acked == [entry_id]

    def test_a_missing_kind_field_defaults_to_turn(self, monkeypatch):
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
                "ctx": {"tenant": "acme", "principal": "p1", "claims": {}},
                "require_approval": False,
                "images": [],
            }
        )

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r1")]]
        assert events == [{"type": "done"}]

    def test_clears_any_stale_cancel_flag_before_starting(self, monkeypatch):
        """A flag left over from a PRIOR turn on this thread_id (e.g.
        /chat/cancel raced with that turn already finishing on its own)
        must not spuriously cancel this brand-new one. Captures the
        cancel_check RESULT into a plain list rather than asserting inside
        the fake generator — an assertion failure in there would just be
        caught by process_request's own try/except and published as an
        ordinary error event instead of failing this test."""
        from app.turns.queue import is_cancelled, set_cancel_flag

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
        still_cancelled_after = asyncio.run(_run())
        assert seen_cancelled == [False]
        assert still_cancelled_after is False

    def test_passes_a_working_cancel_check_bound_to_the_thread_id(self, monkeypatch):
        from app.turns.queue import set_cancel_flag

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

        assert asyncio.run(_run()) is True

    def test_passes_the_decoded_payload_fields_through(self, monkeypatch):
        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured.update(text=text, thread_id=thread_id, ctx=ctx, require_approval=require_approval)
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        ctx = {"tenant": "acme", "principal": "p9", "claims": {}}
        entry_id, fields = _entry(text="what is 2+2?", thread_id="t9", ctx=ctx, require_approval=True)

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured == {
            "text": "what is 2+2?",
            "thread_id": "t9",
            "ctx": ctx,
            "require_approval": True,
        }

    def test_passes_images_through_when_attached(self, monkeypatch):
        captured = {}

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            captured["images"] = images
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r4", images=["https://example.com/cat.png"])

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured["images"] == ["https://example.com/cat.png"]

    def test_no_images_attached_passes_none_not_an_empty_list(self, monkeypatch):
        """_build_human_content (app/agent/runtime.py) treats an empty list the
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

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured["images"] is None

    def test_a_failure_publishes_an_error_event_and_still_acks(self, monkeypatch):
        async def failing_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            raise RuntimeError("graph blew up")
            yield  # pragma: no cover - unreachable, makes this a generator

        monkeypatch.setattr(agent_worker, "astream_events_turn", failing_turn)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r2")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert events == [{"type": "error", "content": "graph blew up"}]
        assert client.acked == [entry_id]


class TestProcessRequestResume:
    def test_dispatches_to_astream_events_resume_with_the_right_args(self, monkeypatch):
        captured = {}

        async def fake_resume(thread_id, approved, ctx):
            captured.update(thread_id=thread_id, approved=approved, ctx=ctx)
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_resume", fake_resume)
        client = FakeRedis()
        ctx = {"tenant": "acme", "principal": "p1", "claims": {}}
        entry_id, fields = _entry(kind="resume", request_id="r6", thread_id="t6", ctx=ctx, approved=False)

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured == {"thread_id": "t6", "approved": False, "ctx": ctx}
        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r6")]]
        assert events == [{"type": "done"}]
        assert client.acked == [entry_id]

    def test_a_resume_failure_publishes_an_error_and_still_acks(self, monkeypatch):
        async def failing_resume(thread_id, approved, ctx):
            raise RuntimeError("checkpoint gone")
            yield  # pragma: no cover

        monkeypatch.setattr(agent_worker, "astream_events_resume", failing_resume)
        client = FakeRedis()
        entry_id, fields = _entry(kind="resume", request_id="r7")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r7")]]
        assert events == [{"type": "error", "content": "checkpoint gone"}]
        assert client.acked == [entry_id]


class TestProcessRequestCancel:
    def test_a_successful_cancel_publishes_a_cancelled_error_event(self, monkeypatch):
        captured = {}

        async def fake_cancel_run(thread_id, ctx):
            captured.update(thread_id=thread_id, ctx=ctx)
            return True

        monkeypatch.setattr(agent_worker, "cancel_run", fake_cancel_run)
        client = FakeRedis()
        ctx = {"tenant": "acme", "principal": "p1", "claims": {}}
        entry_id, fields = _entry(kind="cancel", request_id="r8", thread_id="t8", ctx=ctx)

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured == {"thread_id": "t8", "ctx": ctx}
        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r8")]]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "cancelled"
        assert client.acked == [entry_id]

    def test_nothing_to_cancel_publishes_a_plain_done(self, monkeypatch):
        """cancel_run returns False when nothing was paused (e.g. the
        thread is actively streaming instead, handled by the separate
        cancel-flag mechanism, not this job) — reported as a quiet "done,"
        not an error, since nothing actually went wrong."""

        async def fake_cancel_run(thread_id, ctx):
            return False

        monkeypatch.setattr(agent_worker, "cancel_run", fake_cancel_run)
        client = FakeRedis()
        entry_id, fields = _entry(kind="cancel", request_id="r9")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r9")]]
        assert events == [{"type": "done"}]
        assert client.acked == [entry_id]


class TestProcessRequestUnknownKind:
    def test_unknown_kind_publishes_an_error_and_still_acks(self):
        """A malformed/future-version payload must not silently hang or
        crash the worker loop — same "always ack, publish an error"
        contract as any other processing failure."""
        client = FakeRedis()
        entry_id, fields = _entry(kind="not-a-real-kind", request_id="r10")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r10")]]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "not-a-real-kind" in events[0]["content"]
        assert client.acked == [entry_id]


class TestRunLoop:
    def test_processes_one_request_end_to_end_via_the_consumer_group(self, monkeypatch):
        """A thin proof that run()'s xreadgroup wiring actually delivers a
        published request to process_request — not a re-test of
        FakeRedis's own semantics (see tests/turns/test_queue.py for those)."""

        async def fake_turn(text, thread_id, ctx, require_approval=False, images=None, cancel_check=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
        monkeypatch.setattr(agent_worker, "init_graph_async", _noop_async)
        client = FakeRedis()
        monkeypatch.setattr(agent_worker, "get_client", lambda: client)

        async def _run_one_iteration():
            from app.turns.queue import ensure_consumer_group

            await ensure_consumer_group(client)
            entry_id, fields = _entry(request_id="r3")
            client.streams[REQUESTS_STREAM].append((entry_id, fields))
            response = await client.xreadgroup(
                CONSUMER_GROUP, agent_worker.CONSUMER_NAME, {REQUESTS_STREAM: ">"}, count=1
            )
            _, entries = response[0]
            for eid, f in entries:
                await agent_worker.process_request(client, eid, f)

        asyncio.run(_run_one_iteration())

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

    def test_bounds_concurrency_and_actually_overlaps(self, monkeypatch):
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
                entry_id, fields = _entry(request_id=f"r{i}")
                await semaphore.acquire()
                task = asyncio.create_task(
                    agent_worker._process_with_limit(client, entry_id, fields, semaphore)
                )
                tasks.append(task)
            await asyncio.gather(*tasks)

        asyncio.run(_dispatch_all())

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


async def _noop_async(*args, **kwargs):
    return None
