"""Tests for app/job_queue/queue.py — the Redis Streams queue mechanics between the
SSE-serving process and app/job_queue/agent_worker.py's agent workers
(GRAPH_PATTERNS.md pattern 43). A hand-rolled in-memory fake stands in for
a real Redis Stream (no live Redis needed), matching the rest of this
suite's hermetic discipline.

Every test that drives async queue calls is `async def` (pytest-asyncio's
`asyncio_mode = "auto"`, pyproject.toml) and awaits directly, instead of
each wrapping its own call in `asyncio.run(...)`.
"""
import json

from redis.exceptions import ResponseError

from app.job_queue import queue


def _id_gt(a: str, b: str) -> bool:
    return tuple(int(x) for x in a.split("-")) > tuple(int(x) for x in b.split("-"))


class FakeRedis:
    """An in-memory stand-in for redis.asyncio.Redis covering exactly the
    Streams commands app/job_queue/queue.py/app/job_queue/agent_worker.py actually use."""

    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[str, set[str]] = {}
        self.expiries: dict[str, int] = {}
        self.acked: list[str] = []
        self.deleted: list[str] = []
        self.kv: dict[str, str] = {}  # plain SET/GET/DELETE keys — the cancel flag
        self._counter = 0
        # entry_id -> simulated idle time in ms since last delivery/claim.
        # Real Redis derives this from a wall clock; tests instead set it
        # directly (e.g. `client._delivered[entry_id] = 999_999`) to
        # simulate "abandoned long enough to reclaim" without a fake clock.
        self._delivered: dict[str, int] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}-0"

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        entry_id = self._next_id()
        self.streams.setdefault(stream, []).append((entry_id, dict(fields)))
        if maxlen is not None:
            self.streams[stream] = self.streams[stream][-maxlen:]
        return entry_id

    async def xgroup_create(self, stream, group, id="0", mkstream=False):
        self.streams.setdefault(stream, [])
        groups = self.groups.setdefault(stream, set())
        if group in groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        groups.add(group)

    async def xread(self, streams: dict, block=None, count=None):
        result = []
        for key, last_id in streams.items():
            entries = [(eid, f) for eid, f in self.streams.get(key, []) if _id_gt(eid, last_id)]
            if count:
                entries = entries[:count]
            if entries:
                result.append((key, entries))
        return result

    async def xreadgroup(self, group, consumer, streams: dict, count=None, block=None):
        result = []
        for key in streams:
            entries = [
                (eid, f) for eid, f in self.streams.get(key, []) if eid not in self._delivered
            ]
            if count:
                entries = entries[:count]
            if entries:
                for eid, _ in entries:
                    self._delivered[eid] = 0
                result.append((key, entries))
        return result

    async def xautoclaim(self, stream, group, consumer, min_idle_time, start_id="0-0", count=None):
        """Just enough of real XAUTOCLAIM for queue.py::reclaim_stale_entries:
        claims (and returns) every pending, unacked entry whose simulated
        idle time (see `_delivered`'s own docstring) is at least
        `min_idle_time` ms. Always drains in one pass (returns cursor
        "0-0"), unlike real Redis's paginated form — reclaim_stale_entries's
        own loop still works correctly against that, it just never repeats."""
        claimed = [
            (eid, f)
            for eid, f in self.streams.get(stream, [])
            if eid in self._delivered and eid not in self.acked and self._delivered[eid] >= min_idle_time
        ]
        if count:
            claimed = claimed[:count]
        for eid, _ in claimed:
            self._delivered[eid] = 0  # claimed entries reset to freshly-delivered
        return ["0-0", claimed, []]

    async def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds

    async def delete(self, key):
        self.deleted.append(key)
        self.streams.pop(key, None)
        self.kv.pop(key, None)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def eval(self, script, numkeys, *keys_and_args):
        """Only ever called with queue.py's own `_RELEASE_LOCK_SCRIPT`
        (compare-and-delete) — reimplements just that, not a real Lua
        interpreter, same "just enough" scope as every other method here."""
        key, token = keys_and_args[0], keys_and_args[1]
        if self.kv.get(key) == token:
            del self.kv[key]
            return 1
        return 0


class TestGetClient:
    def test_disables_the_socket_timeout_so_a_blocking_read_cannot_race_it(self, monkeypatch):
        """Regression guard: redis-py defaults socket_timeout to 5 seconds
        — verified empirically (a real `make agent-worker` smoke test
        against this app's own docker-compose Redis) that this races
        directly against XREAD/XREADGROUP's server-side BLOCK (5000ms
        here), so the client-side socket timed out and raised
        redis.exceptions.TimeoutError before Redis's own block window
        ever elapsed, on every real blocking read. socket_timeout=None
        is what fixes it; this test exists so a future "cleanup" can't
        silently drop that kwarg and reintroduce the race."""
        monkeypatch.setattr(queue, "_client", None)
        captured = {}

        class _FakeRedisClient:
            def __init__(self, *a, **kw):
                pass

        def fake_from_url(url, **kwargs):
            captured.update(kwargs)
            return _FakeRedisClient()

        monkeypatch.setattr(queue.redis.Redis, "from_url", staticmethod(fake_from_url))

        queue.get_client()

        assert captured.get("socket_timeout") is None
        assert "socket_timeout" in captured  # explicitly passed, not just absent


class TestEnsureConsumerGroup:
    async def test_creates_the_group_on_first_call(self):
        client = FakeRedis()
        await queue.ensure_consumer_group(client)
        assert queue.CONSUMER_GROUP in client.groups[queue.requests_stream_key("ecorp")]

    async def test_a_different_domain_gets_its_own_group_on_its_own_stream(self):
        client = FakeRedis()
        await queue.ensure_consumer_group(client, "support")
        assert queue.CONSUMER_GROUP in client.groups[queue.requests_stream_key("support")]
        # Never touched Ecorp's own stream/group at all.
        assert queue.requests_stream_key("ecorp") not in client.groups

    async def test_is_idempotent_a_second_call_does_not_raise(self):
        client = FakeRedis()
        await queue.ensure_consumer_group(client)
        await queue.ensure_consumer_group(client)  # must not raise BUSYGROUP


class TestPublishRequest:
    async def test_enqueues_a_json_payload_with_every_field(self):
        client = FakeRedis()
        await queue.publish_request(
            client,
            request_id="r1",
            text="hello",
            thread_id="t1",
            ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
            require_approval=True,
        )
        entries = client.streams[queue.requests_stream_key("ecorp")]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "turn",
            "request_id": "r1",
            "text": "hello",
            "thread_id": "t1",
            "ctx": {"tenant": "ecorp", "principal": "p1", "claims": {}},
            "require_approval": True,
            "images": [],
        }

    async def test_carries_images_when_attached(self):
        client = FakeRedis()
        await queue.publish_request(
            client,
            request_id="r2",
            text="what is in this image?",
            thread_id="t1",
            ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
            images=["data:image/png;base64,abc123"],
        )
        payload = json.loads(client.streams[queue.requests_stream_key("ecorp")][0][1]["payload"])
        assert payload["images"] == ["data:image/png;base64,abc123"]

    async def test_a_non_default_domain_lands_on_its_own_stream_not_ecorps(self):
        client = FakeRedis()
        await queue.publish_request(
            client,
            request_id="r3",
            text="my order hasn't shipped",
            thread_id="t1",
            ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
            domain="support",
        )
        assert queue.requests_stream_key("ecorp") not in client.streams
        entries = client.streams[queue.requests_stream_key("support")]
        assert len(entries) == 1
        assert json.loads(entries[0][1]["payload"])["request_id"] == "r3"


class TestPublishResumeRequest:
    async def test_enqueues_a_resume_job_onto_the_same_stream(self):
        """Same domain's requests stream as a new turn — one consumer
        group, one dispatch-by-kind in app/job_queue/agent_worker.py, not a
        second queue."""
        client = FakeRedis()
        await queue.publish_resume_request(
            client,
            request_id="r1",
            thread_id="t1",
            approved=True,
            ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
        )
        entries = client.streams[queue.requests_stream_key("ecorp")]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "resume",
            "request_id": "r1",
            "thread_id": "t1",
            "approved": True,
            "ctx": {"tenant": "ecorp", "principal": "p1", "claims": {}},
        }


class TestPublishCancelRequest:
    async def test_enqueues_a_cancel_job_onto_the_same_stream(self):
        client = FakeRedis()
        await queue.publish_cancel_request(
            client,
            request_id="r1",
            thread_id="t1",
            ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
        )
        entries = client.streams[queue.requests_stream_key("ecorp")]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "cancel",
            "request_id": "r1",
            "thread_id": "t1",
            "ctx": {"tenant": "ecorp", "principal": "p1", "claims": {}},
        }


class TestCancelFlag:
    """The separate, per-thread (not per-request) mechanism POST
    /chat/cancel uses to stop an ACTIVELY STREAMING turn — see
    app/job_queue/agent_worker.py's cancel_check wiring."""

    async def test_is_cancelled_false_before_anything_is_set(self):
        client = FakeRedis()
        assert await queue.is_cancelled(client, "t1") is False

    async def test_set_then_is_cancelled_true(self):
        client = FakeRedis()
        await queue.set_cancel_flag(client, "t1")
        assert await queue.is_cancelled(client, "t1") is True

    async def test_set_flag_is_scoped_to_its_own_thread_id(self):
        client = FakeRedis()
        await queue.set_cancel_flag(client, "t1")
        assert await queue.is_cancelled(client, "t2") is False

    async def test_clear_removes_the_flag(self):
        client = FakeRedis()
        await queue.set_cancel_flag(client, "t1")
        await queue.clear_cancel_flag(client, "t1")
        assert await queue.is_cancelled(client, "t1") is False

    async def test_clear_on_a_never_set_thread_id_does_not_raise(self):
        client = FakeRedis()
        await queue.clear_cancel_flag(client, "never-set")  # must not raise


class TestThreadLock:
    """Mutual exclusion across job kinds on one thread_id — see queue.py's
    own module docstring for the checkpoint-fork race this exists to
    close."""

    async def test_acquire_succeeds_when_free(self):
        client = FakeRedis()
        assert await queue.acquire_thread_lock(client, "t1", "token-a") is True

    async def test_a_second_acquire_while_held_fails(self):
        client = FakeRedis()
        assert await queue.acquire_thread_lock(client, "t1", "token-a") is True
        assert await queue.acquire_thread_lock(client, "t1", "token-b") is False

    async def test_acquire_sets_a_ttl_so_a_crashed_holder_self_heals(self):
        client = FakeRedis()
        await queue.acquire_thread_lock(client, "t1", "token-a")
        assert client.expiries[queue.thread_lock_key("t1")] == queue.THREAD_LOCK_TTL_SECONDS

    async def test_release_then_acquire_by_someone_else_succeeds(self):
        client = FakeRedis()
        await queue.acquire_thread_lock(client, "t1", "token-a")
        await queue.release_thread_lock(client, "t1", "token-a")
        assert await queue.acquire_thread_lock(client, "t1", "token-b") is True

    async def test_release_with_the_wrong_token_does_not_remove_a_different_holders_lock(self):
        """The scenario the compare-and-delete script exists for: token-a's
        own lock already expired and token-b legitimately acquired it —
        token-a's (late) release must not evict token-b."""
        client = FakeRedis()
        await queue.acquire_thread_lock(client, "t1", "token-a")
        del client.kv[queue.thread_lock_key("t1")]  # simulate TTL expiry
        await queue.acquire_thread_lock(client, "t1", "token-b")

        await queue.release_thread_lock(client, "t1", "token-a")

        assert client.kv[queue.thread_lock_key("t1")] == "token-b"

    async def test_locks_are_scoped_to_their_own_thread_id(self):
        client = FakeRedis()
        await queue.acquire_thread_lock(client, "t1", "token-a")
        assert await queue.acquire_thread_lock(client, "t2", "token-b") is True

    async def test_release_on_a_never_acquired_thread_id_does_not_raise(self):
        client = FakeRedis()
        await queue.release_thread_lock(client, "never-locked", "token-a")  # must not raise


class TestPublishResultAndReadResults:
    async def test_read_results_yields_events_in_order_and_stops_at_done(self):
        client = FakeRedis()
        await queue.publish_result(client, "r1", {"type": "token", "content": "Hel"})
        await queue.publish_result(client, "r1", {"type": "token", "content": "lo"})
        await queue.publish_result(client, "r1", {"type": "done"})
        events = [event async for event in queue.read_results(client, "r1")]
        assert events == [
            {"type": "token", "content": "Hel"},
            {"type": "token", "content": "lo"},
            {"type": "done"},
        ]

    async def test_read_results_stops_at_an_error_event_too(self):
        client = FakeRedis()
        await queue.publish_result(client, "r1", {"type": "error", "content": "boom"})
        events = [event async for event in queue.read_results(client, "r1")]
        assert events == [{"type": "error", "content": "boom"}]

    async def test_read_results_stops_at_approval_required_too(self):
        """approval_required is the last event a "turn" job's worker ever
        publishes for a paused turn (app/agent/runtime_stream.py::_run_graph_stream never
        yields anything after it) — without treating it as terminal here,
        this generator would block forever waiting for a "done"/"error"
        that will never come on THIS results stream (resuming is a
        separate job with its own — see publish_resume_request above)."""
        client = FakeRedis()
        await queue.publish_result(client, "r1", {"type": "token", "content": "hi"})
        await queue.publish_result(
            client, "r1", {"type": "approval_required", "tool_calls": []}
        )
        events = [event async for event in queue.read_results(client, "r1")]
        assert events == [
            {"type": "token", "content": "hi"},
            {"type": "approval_required", "tool_calls": []},
        ]

    async def test_read_results_ignores_events_from_a_different_request(self):
        client = FakeRedis()
        await queue.publish_result(client, "other", {"type": "token", "content": "nope"})
        await queue.publish_result(client, "r1", {"type": "done"})
        events = [event async for event in queue.read_results(client, "r1")]
        assert events == [{"type": "done"}]

    async def test_publish_result_refreshes_the_ttl_on_every_write(self):
        client = FakeRedis()
        await queue.publish_result(client, "r1", {"type": "token", "content": "a"})
        await queue.publish_result(client, "r1", {"type": "done"})
        assert client.expiries[queue.results_stream_key("r1")] == queue.RESULTS_STREAM_TTL_SECONDS


class TestDeleteResultsStream:
    async def test_deletes_the_key(self):
        client = FakeRedis()
        await queue.publish_result(client, "r1", {"type": "done"})
        await queue.delete_results_stream(client, "r1")
        assert queue.results_stream_key("r1") in client.deleted

    async def test_never_raises_even_if_the_client_errors(self):
        class _RaisingClient(FakeRedis):
            async def delete(self, key):
                raise RuntimeError("connection reset")

        await queue.delete_results_stream(_RaisingClient(), "r1")  # must not raise


class TestReclaimStaleEntries:
    """queue.py's crash-recovery primitive: finds entries a dead worker
    claimed but never acked. See app/job_queue/agent_worker.py's/
    app/ingestion/ingest_worker.py's own `_reclaim_loop` for the policy
    built on top of this (never blindly redeliver — surface + dead-letter);
    this only tests the Streams mechanics."""

    async def test_an_entry_idle_past_the_threshold_is_claimed(self):
        client = FakeRedis()
        entry_id = await client.xadd("s", {"payload": "p1"})
        await client.xreadgroup("g", "c1", {"s": ">"})  # delivered, now pending
        client._delivered[entry_id] = 999_999  # simulate: abandoned a long time ago

        claimed = await queue.reclaim_stale_entries(
            client, stream="s", group="g", consumer="c2", min_idle_ms=100
        )

        assert [eid for eid, _ in claimed] == [entry_id]

    async def test_an_entry_still_within_the_idle_threshold_is_left_alone(self):
        """A job genuinely still being worked (not yet idle long enough to
        presume its worker dead) must not be swept up — that would race a
        live, in-progress handler."""
        client = FakeRedis()
        entry_id = await client.xadd("s", {"payload": "p1"})
        await client.xreadgroup("g", "c1", {"s": ">"})
        client._delivered[entry_id] = 50  # only just delivered

        claimed = await queue.reclaim_stale_entries(
            client, stream="s", group="g", consumer="c2", min_idle_ms=100_000
        )

        assert claimed == []

    async def test_an_already_acked_entry_is_never_reclaimed(self):
        client = FakeRedis()
        entry_id = await client.xadd("s", {"payload": "p1"})
        await client.xreadgroup("g", "c1", {"s": ">"})
        client._delivered[entry_id] = 999_999
        await client.xack("s", "g", entry_id)

        claimed = await queue.reclaim_stale_entries(
            client, stream="s", group="g", consumer="c2", min_idle_ms=100
        )

        assert claimed == []

    async def test_a_never_delivered_entry_is_not_reclaimed(self):
        """A fresh, still-queued entry (never even read by xreadgroup) has
        no pending-entry idle time at all — nothing to reclaim."""
        client = FakeRedis()
        await client.xadd("s", {"payload": "p1"})

        claimed = await queue.reclaim_stale_entries(
            client, stream="s", group="g", consumer="c2", min_idle_ms=0
        )

        assert claimed == []


class TestPublishDeadLetter:
    async def test_archives_the_payload_under_the_streams_own_dead_letter_key(self):
        client = FakeRedis()
        await queue.publish_dead_letter(
            client,
            requests_stream=queue.requests_stream_key("ecorp"),
            entry_id="7-0",
            payload={"kind": "turn", "request_id": "r1"},
            reason="worker_lost",
        )
        entries = client.streams[queue.dead_letter_stream_key(queue.requests_stream_key("ecorp"))]
        assert len(entries) == 1
        fields = entries[0][1]
        assert fields["original_entry_id"] == "7-0"
        assert fields["reason"] == "worker_lost"
        assert json.loads(fields["payload"]) == {"kind": "turn", "request_id": "r1"}

    async def test_is_capped_by_maxlen(self):
        client = FakeRedis()
        for i in range(queue.DEAD_LETTER_MAXLEN + 10):
            await queue.publish_dead_letter(
                client,
                requests_stream="s",
                entry_id=f"{i}-0",
                payload={},
                reason="worker_lost",
            )
        assert len(client.streams[queue.dead_letter_stream_key("s")]) <= queue.DEAD_LETTER_MAXLEN
