"""Tests for app/adapters/outbound/redis/ingest_queue.py — the production ingestion pipeline's
own Redis Streams queue, deliberately separate from app/adapters/outbound/redis/queue.py's
chat-turn queue (see its module docstring for why). Reuses
tests/adapters/outbound/redis/test_queue.py's FakeRedis — the same in-memory Streams stand-in
works unchanged since ingest_queue.py talks to Redis the same way
queue.py does, just against different stream/group names.
"""
import json

import pytest

from app.adapters.inbound.workers import ingest_streams
from app.adapters.outbound.redis import ingest_queue
from app.ports.outbound import jobs
from tests.adapters.outbound.redis.test_queue import FakeRedis


class TestEnsureConsumerGroup:
    async def test_creates_the_group_on_first_call(self):
        client = FakeRedis()
        await ingest_streams.ensure_consumer_group(client)
        assert jobs.INGEST_CONSUMER_GROUP in client.groups[jobs.INGEST_REQUESTS_STREAM]

    async def test_is_idempotent(self):
        client = FakeRedis()
        await ingest_streams.ensure_consumer_group(client)
        await ingest_streams.ensure_consumer_group(client)  # must not raise BUSYGROUP

    def test_uses_a_separate_stream_and_group_from_the_chat_queue(self):
        """The whole point of this module existing separately — verified
        directly, not just asserted by convention."""

        assert jobs.INGEST_REQUESTS_STREAM != jobs.requests_stream_key()
        assert jobs.INGEST_CONSUMER_GROUP != jobs.CONSUMER_GROUP


class TestPublishIngestRequest:
    async def test_enqueues_a_json_payload_with_every_field(self):
        client = FakeRedis()
        ctx = {"tenant": "ecorp", "principal": "p1", "claims": {}}
        await ingest_queue.publish_ingest_request(
                client,
                job_id="j1",
                object_key="ecorp/abc-report.pdf",
                filename="report.pdf",
                content_type="application/pdf",
                ctx=ctx,
                topic="company",
            )
        entries = client.streams[jobs.INGEST_REQUESTS_STREAM]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "job_id": "j1",
            "object_key": "ecorp/abc-report.pdf",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "ctx": ctx,
            "topic": "company",
        }

    async def test_topic_defaults_to_none(self):
        client = FakeRedis()
        await ingest_queue.publish_ingest_request(
                client,
                job_id="j2",
                object_key="k",
                filename="notes.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ctx={"tenant": "ecorp", "principal": "p1", "claims": {}},
            )
        payload = json.loads(client.streams[jobs.INGEST_REQUESTS_STREAM][0][1]["payload"])
        assert payload["topic"] is None


class TestPublishResultAndReadResults:
    async def test_read_results_yields_events_in_order_and_stops_at_done(self):
        client = FakeRedis()

        async def _run():
            await ingest_streams.publish_result(client, "j1", {"type": "started"})
            await ingest_streams.publish_result(client, "j1", {"type": "done", "chunks": 12})
            return [event async for event in ingest_queue.read_results(client, "j1")]

        events = await _run()
        assert events == [{"type": "started"}, {"type": "done", "chunks": 12}]

    async def test_read_results_stops_at_an_error_event_too(self):
        client = FakeRedis()

        async def _run():
            await ingest_streams.publish_result(client, "j1", {"type": "error", "content": "bad file"})
            return [event async for event in ingest_queue.read_results(client, "j1")]

        assert await _run() == [{"type": "error", "content": "bad file"}]

    async def test_publish_result_refreshes_the_ttl(self):
        client = FakeRedis()
        await ingest_streams.publish_result(client, "j1", {"type": "done", "chunks": 1})
        assert (
            client.expiries[jobs.ingest_results_stream_key("j1")]
            == jobs.RESULTS_STREAM_TTL_SECONDS
        )

    async def test_a_first_event_deadline_yields_one_error_event_and_stops(self):
        """Same fix as app/adapters/outbound/redis/queue.py::read_results — a job_id no
        ingest-worker ever picks up must not hang GET /ingest/stream/{job_id}
        forever."""
        client = FakeRedis()  # nothing ever published to "j1"
        events = [
            event
            async for event in ingest_queue.read_results(client, "j1", first_event_deadline_seconds=0.05)
        ]
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "j1" in events[0]["content"]

    async def test_a_first_event_deadline_never_fires_once_a_real_event_has_arrived(self):
        client = FakeRedis()
        await ingest_streams.publish_result(client, "j1", {"type": "started"})
        await ingest_streams.publish_result(client, "j1", {"type": "done", "chunks": 1})

        events = [
            event
            async for event in ingest_queue.read_results(client, "j1", first_event_deadline_seconds=0.001)
        ]

        assert events == [{"type": "started"}, {"type": "done", "chunks": 1}]


class TestDeleteResultsStream:
    async def test_deletes_the_key(self):
        client = FakeRedis()
        await ingest_streams.publish_result(client, "j1", {"type": "done", "chunks": 1})
        await ingest_queue.delete_results_stream(client, "j1")
        assert jobs.ingest_results_stream_key("j1") in client.deleted

    async def test_never_raises_even_if_the_client_errors(self):
        class _RaisingClient(FakeRedis):
            async def delete(self, key):
                raise RuntimeError("connection reset")

        await ingest_queue.delete_results_stream(_RaisingClient(), "j1")  # must not raise


class TestIngestQueueAdapter:
    """`RedisIngestQueue` is the `IngestQueue` port (`app/ports/outbound/jobs.py`) over this module's functions: the upload route reaches the queue only through it."""

    @pytest.fixture
    def adapter(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(ingest_queue, "get_client", lambda: client)
        return ingest_queue.RedisIngestQueue(), client

    async def test_a_published_job_carries_a_pointer_to_the_object_not_the_bytes(self, adapter):
        queue_port, client = adapter

        await queue_port.publish_ingest_request(
            job_id="j1", object_key="acme/j1-a.pdf", filename="a.pdf", content_type="application/pdf",
            ctx={"tenant": "acme", "principal": "p", "claims": {}}, topic="hr",
        )

        payload = json.loads(client.streams[jobs.INGEST_REQUESTS_STREAM][0][1]["payload"])
        assert (payload["job_id"], payload["object_key"], payload["topic"]) == ("j1", "acme/j1-a.pdf", "hr")

    async def test_a_jobs_events_are_read_until_it_ends_and_its_stream_can_be_deleted(self, adapter):
        queue_port, client = adapter
        await ingest_streams.publish_result(client, "j1", {"type": "started"})
        await ingest_streams.publish_result(client, "j1", {"type": "done", "chunks": 3})

        assert [e["type"] async for e in queue_port.read_results("j1")] == ["started", "done"]
        await queue_port.delete_results_stream("j1")
        assert jobs.ingest_results_stream_key("j1") not in client.streams
