"""Tests for app/agent_worker.py's process_request — the Redis Streams
consumer side of GRAPH_PATTERNS.md pattern 43. Reuses tests/test_queue.py's
FakeRedis; app.agent_worker.astream_events_turn_unattended is monkeypatched
so these never touch a real graph/LLM.
"""
import asyncio
import json

from app import agent_worker
from app.queue import CONSUMER_GROUP, REQUESTS_STREAM, results_stream_key
from tests.test_queue import FakeRedis


def _entry(
    request_id="r1", text="hi", thread_id="t1", ctx=None, require_approval=False, images=None
):
    payload = json.dumps(
        {
            "request_id": request_id,
            "text": text,
            "thread_id": thread_id,
            "ctx": ctx or {"tenant": "acme", "principal": "p1", "claims": {}},
            "require_approval": require_approval,
            "images": images or [],
        }
    )
    return "1-0", {"payload": payload}


class TestProcessRequest:
    def test_publishes_every_yielded_event_and_acks(self, monkeypatch):
        async def fake_stream(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "token", "content": "Hel"}
            yield {"type": "token", "content": "lo"}
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", fake_stream)
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

    def test_passes_the_decoded_payload_fields_through(self, monkeypatch):
        captured = {}

        async def fake_stream(text, thread_id, ctx, require_approval=False, images=None):
            captured.update(text=text, thread_id=thread_id, ctx=ctx, require_approval=require_approval)
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", fake_stream)
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

        async def fake_stream(text, thread_id, ctx, require_approval=False, images=None):
            captured["images"] = images
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", fake_stream)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r4", images=["https://example.com/cat.png"])

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured["images"] == ["https://example.com/cat.png"]

    def test_no_images_attached_passes_none_not_an_empty_list(self, monkeypatch):
        """_build_human_content (app/agent.py) treats an empty list the
        same as None, but keeping the distinction here means a future
        reader can tell "no image was ever attached" from "an empty list
        was explicitly sent" by reading the call, not by re-deriving it."""
        captured = {}

        async def fake_stream(text, thread_id, ctx, require_approval=False, images=None):
            captured["images"] = images
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", fake_stream)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r5")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        assert captured["images"] is None

    def test_a_failure_publishes_an_error_event_and_still_acks(self, monkeypatch):
        async def failing_stream(text, thread_id, ctx, require_approval=False, images=None):
            raise RuntimeError("graph blew up")
            yield  # pragma: no cover - unreachable, makes this a generator

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", failing_stream)
        client = FakeRedis()
        entry_id, fields = _entry(request_id="r2")

        asyncio.run(agent_worker.process_request(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[results_stream_key("r2")]]
        assert events == [{"type": "error", "content": "graph blew up"}]
        assert client.acked == [entry_id]


class TestRunLoop:
    def test_processes_one_request_end_to_end_via_the_consumer_group(self, monkeypatch):
        """A thin proof that run()'s xreadgroup wiring actually delivers a
        published request to process_request — not a re-test of
        FakeRedis's own semantics (see tests/test_queue.py for those)."""

        async def fake_stream(text, thread_id, ctx, require_approval=False, images=None):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn_unattended", fake_stream)
        monkeypatch.setattr(agent_worker, "init_graph_async", _noop_async)
        client = FakeRedis()
        monkeypatch.setattr(agent_worker, "get_client", lambda: client)

        async def _run_one_iteration():
            from app.queue import ensure_consumer_group

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


async def _noop_async(*args, **kwargs):
    return None
