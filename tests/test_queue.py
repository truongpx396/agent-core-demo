"""Tests for app/queue.py — the Redis Streams queue mechanics between the
SSE-serving process and app/agent_worker.py's agent workers
(GRAPH_PATTERNS.md pattern 43). A hand-rolled in-memory fake stands in for
a real Redis Stream (no live Redis needed), matching the rest of this
suite's hermetic discipline.

No pytest-asyncio plugin is installed in this project (see
tests/test_durable_checkpoint.py) — async behavior is driven the same
established way: a plain sync `def test_...` wrapping an inner `async def`
closure via `asyncio.run(...)`.
"""
import asyncio
import json

from redis.exceptions import ResponseError

from app import queue


def _id_gt(a: str, b: str) -> bool:
    return tuple(int(x) for x in a.split("-")) > tuple(int(x) for x in b.split("-"))


class FakeRedis:
    """An in-memory stand-in for redis.asyncio.Redis covering exactly the
    Streams commands app/queue.py/app/agent_worker.py actually use."""

    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[str, set[str]] = {}
        self.expiries: dict[str, int] = {}
        self.acked: list[str] = []
        self.deleted: list[str] = []
        self.kv: dict[str, str] = {}  # plain SET/GET/DELETE keys — the cancel flag
        self._counter = 0
        self._delivered: set[str] = set()

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}-0"

    async def xadd(self, stream, fields):
        entry_id = self._next_id()
        self.streams.setdefault(stream, []).append((entry_id, dict(fields)))
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
                    self._delivered.add(eid)
                result.append((key, entries))
        return result

    async def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds

    async def delete(self, key):
        self.deleted.append(key)
        self.streams.pop(key, None)
        self.kv.pop(key, None)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)


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
    def test_creates_the_group_on_first_call(self):
        client = FakeRedis()
        asyncio.run(queue.ensure_consumer_group(client))
        assert queue.CONSUMER_GROUP in client.groups[queue.REQUESTS_STREAM]

    def test_is_idempotent_a_second_call_does_not_raise(self):
        client = FakeRedis()
        asyncio.run(queue.ensure_consumer_group(client))
        asyncio.run(queue.ensure_consumer_group(client))  # must not raise BUSYGROUP


class TestPublishRequest:
    def test_enqueues_a_json_payload_with_every_field(self):
        client = FakeRedis()
        asyncio.run(
            queue.publish_request(
                client,
                request_id="r1",
                text="hello",
                thread_id="t1",
                ctx={"tenant": "acme", "principal": "p1", "claims": {}},
                require_approval=True,
            )
        )
        entries = client.streams[queue.REQUESTS_STREAM]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "turn",
            "request_id": "r1",
            "text": "hello",
            "thread_id": "t1",
            "ctx": {"tenant": "acme", "principal": "p1", "claims": {}},
            "require_approval": True,
            "images": [],
        }

    def test_carries_images_when_attached(self):
        client = FakeRedis()
        asyncio.run(
            queue.publish_request(
                client,
                request_id="r2",
                text="what is in this image?",
                thread_id="t1",
                ctx={"tenant": "acme", "principal": "p1", "claims": {}},
                images=["data:image/png;base64,abc123"],
            )
        )
        payload = json.loads(client.streams[queue.REQUESTS_STREAM][0][1]["payload"])
        assert payload["images"] == ["data:image/png;base64,abc123"]


class TestPublishResumeRequest:
    def test_enqueues_a_resume_job_onto_the_same_stream(self):
        """Same REQUESTS_STREAM as a new turn — one consumer group, one
        dispatch-by-kind in app/agent_worker.py, not a second queue."""
        client = FakeRedis()
        asyncio.run(
            queue.publish_resume_request(
                client,
                request_id="r1",
                thread_id="t1",
                approved=True,
                ctx={"tenant": "acme", "principal": "p1", "claims": {}},
            )
        )
        entries = client.streams[queue.REQUESTS_STREAM]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "resume",
            "request_id": "r1",
            "thread_id": "t1",
            "approved": True,
            "ctx": {"tenant": "acme", "principal": "p1", "claims": {}},
        }


class TestPublishCancelRequest:
    def test_enqueues_a_cancel_job_onto_the_same_stream(self):
        client = FakeRedis()
        asyncio.run(
            queue.publish_cancel_request(
                client,
                request_id="r1",
                thread_id="t1",
                ctx={"tenant": "acme", "principal": "p1", "claims": {}},
            )
        )
        entries = client.streams[queue.REQUESTS_STREAM]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "kind": "cancel",
            "request_id": "r1",
            "thread_id": "t1",
            "ctx": {"tenant": "acme", "principal": "p1", "claims": {}},
        }


class TestCancelFlag:
    """The separate, per-thread (not per-request) mechanism POST
    /chat/cancel uses to stop an ACTIVELY STREAMING turn — see
    app/agent_worker.py's cancel_check wiring."""

    def test_is_cancelled_false_before_anything_is_set(self):
        client = FakeRedis()
        assert asyncio.run(queue.is_cancelled(client, "t1")) is False

    def test_set_then_is_cancelled_true(self):
        client = FakeRedis()

        async def _run():
            await queue.set_cancel_flag(client, "t1")
            return await queue.is_cancelled(client, "t1")

        assert asyncio.run(_run()) is True

    def test_set_flag_is_scoped_to_its_own_thread_id(self):
        client = FakeRedis()

        async def _run():
            await queue.set_cancel_flag(client, "t1")
            return await queue.is_cancelled(client, "t2")

        assert asyncio.run(_run()) is False

    def test_clear_removes_the_flag(self):
        client = FakeRedis()

        async def _run():
            await queue.set_cancel_flag(client, "t1")
            await queue.clear_cancel_flag(client, "t1")
            return await queue.is_cancelled(client, "t1")

        assert asyncio.run(_run()) is False

    def test_clear_on_a_never_set_thread_id_does_not_raise(self):
        client = FakeRedis()
        asyncio.run(queue.clear_cancel_flag(client, "never-set"))  # must not raise


class TestPublishResultAndReadResults:
    def test_read_results_yields_events_in_order_and_stops_at_done(self):
        client = FakeRedis()

        async def _run():
            await queue.publish_result(client, "r1", {"type": "token", "content": "Hel"})
            await queue.publish_result(client, "r1", {"type": "token", "content": "lo"})
            await queue.publish_result(client, "r1", {"type": "done"})
            return [event async for event in queue.read_results(client, "r1")]

        events = asyncio.run(_run())
        assert events == [
            {"type": "token", "content": "Hel"},
            {"type": "token", "content": "lo"},
            {"type": "done"},
        ]

    def test_read_results_stops_at_an_error_event_too(self):
        client = FakeRedis()

        async def _run():
            await queue.publish_result(client, "r1", {"type": "error", "content": "boom"})
            return [event async for event in queue.read_results(client, "r1")]

        assert asyncio.run(_run()) == [{"type": "error", "content": "boom"}]

    def test_read_results_stops_at_approval_required_too(self):
        """approval_required is the last event a "turn" job's worker ever
        publishes for a paused turn (app/agent.py::_run_graph_stream never
        yields anything after it) — without treating it as terminal here,
        this generator would block forever waiting for a "done"/"error"
        that will never come on THIS results stream (resuming is a
        separate job with its own — see publish_resume_request above)."""
        client = FakeRedis()

        async def _run():
            await queue.publish_result(client, "r1", {"type": "token", "content": "hi"})
            await queue.publish_result(
                client, "r1", {"type": "approval_required", "tool_calls": []}
            )
            return [event async for event in queue.read_results(client, "r1")]

        events = asyncio.run(_run())
        assert events == [
            {"type": "token", "content": "hi"},
            {"type": "approval_required", "tool_calls": []},
        ]

    def test_read_results_ignores_events_from_a_different_request(self):
        client = FakeRedis()

        async def _run():
            await queue.publish_result(client, "other", {"type": "token", "content": "nope"})
            await queue.publish_result(client, "r1", {"type": "done"})
            return [event async for event in queue.read_results(client, "r1")]

        assert asyncio.run(_run()) == [{"type": "done"}]

    def test_publish_result_refreshes_the_ttl_on_every_write(self):
        client = FakeRedis()
        asyncio.run(queue.publish_result(client, "r1", {"type": "token", "content": "a"}))
        asyncio.run(queue.publish_result(client, "r1", {"type": "done"}))
        assert client.expiries[queue.results_stream_key("r1")] == queue.RESULTS_STREAM_TTL_SECONDS


class TestDeleteResultsStream:
    def test_deletes_the_key(self):
        client = FakeRedis()
        asyncio.run(queue.publish_result(client, "r1", {"type": "done"}))
        asyncio.run(queue.delete_results_stream(client, "r1"))
        assert queue.results_stream_key("r1") in client.deleted

    def test_never_raises_even_if_the_client_errors(self):
        class _RaisingClient(FakeRedis):
            async def delete(self, key):
                raise RuntimeError("connection reset")

        asyncio.run(queue.delete_results_stream(_RaisingClient(), "r1"))  # must not raise
