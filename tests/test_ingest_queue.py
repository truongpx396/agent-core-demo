"""Tests for app/ingest_queue.py — the production ingestion pipeline's
own Redis Streams queue, deliberately separate from app/queue.py's
chat-turn queue (see its module docstring for why). Reuses
tests/test_queue.py's FakeRedis — the same in-memory Streams stand-in
works unchanged since ingest_queue.py talks to Redis the same way
queue.py does, just against different stream/group names.
"""
import asyncio
import json

from app import ingest_queue
from tests.test_queue import FakeRedis


class TestEnsureConsumerGroup:
    def test_creates_the_group_on_first_call(self):
        client = FakeRedis()
        asyncio.run(ingest_queue.ensure_consumer_group(client))
        assert ingest_queue.INGEST_CONSUMER_GROUP in client.groups[ingest_queue.INGEST_REQUESTS_STREAM]

    def test_is_idempotent(self):
        client = FakeRedis()
        asyncio.run(ingest_queue.ensure_consumer_group(client))
        asyncio.run(ingest_queue.ensure_consumer_group(client))  # must not raise BUSYGROUP

    def test_uses_a_separate_stream_and_group_from_the_chat_queue(self):
        """The whole point of this module existing separately — verified
        directly, not just asserted by convention."""
        from app import queue

        assert ingest_queue.INGEST_REQUESTS_STREAM != queue.REQUESTS_STREAM
        assert ingest_queue.INGEST_CONSUMER_GROUP != queue.CONSUMER_GROUP


class TestPublishIngestRequest:
    def test_enqueues_a_json_payload_with_every_field(self):
        client = FakeRedis()
        ctx = {"tenant": "acme", "principal": "p1", "claims": {}}
        asyncio.run(
            ingest_queue.publish_ingest_request(
                client,
                job_id="j1",
                object_key="acme/abc-report.pdf",
                filename="report.pdf",
                content_type="application/pdf",
                ctx=ctx,
                topic="company",
            )
        )
        entries = client.streams[ingest_queue.INGEST_REQUESTS_STREAM]
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        assert payload == {
            "job_id": "j1",
            "object_key": "acme/abc-report.pdf",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "ctx": ctx,
            "topic": "company",
        }

    def test_topic_defaults_to_none(self):
        client = FakeRedis()
        asyncio.run(
            ingest_queue.publish_ingest_request(
                client,
                job_id="j2",
                object_key="k",
                filename="notes.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ctx={"tenant": "acme", "principal": "p1", "claims": {}},
            )
        )
        payload = json.loads(client.streams[ingest_queue.INGEST_REQUESTS_STREAM][0][1]["payload"])
        assert payload["topic"] is None


class TestPublishResultAndReadResults:
    def test_read_results_yields_events_in_order_and_stops_at_done(self):
        client = FakeRedis()

        async def _run():
            await ingest_queue.publish_result(client, "j1", {"type": "started"})
            await ingest_queue.publish_result(client, "j1", {"type": "done", "chunks": 12})
            return [event async for event in ingest_queue.read_results(client, "j1")]

        events = asyncio.run(_run())
        assert events == [{"type": "started"}, {"type": "done", "chunks": 12}]

    def test_read_results_stops_at_an_error_event_too(self):
        client = FakeRedis()

        async def _run():
            await ingest_queue.publish_result(client, "j1", {"type": "error", "content": "bad file"})
            return [event async for event in ingest_queue.read_results(client, "j1")]

        assert asyncio.run(_run()) == [{"type": "error", "content": "bad file"}]

    def test_publish_result_refreshes_the_ttl(self):
        client = FakeRedis()
        asyncio.run(ingest_queue.publish_result(client, "j1", {"type": "done", "chunks": 1}))
        assert (
            client.expiries[ingest_queue.results_stream_key("j1")]
            == ingest_queue.RESULTS_STREAM_TTL_SECONDS
        )


class TestDeleteResultsStream:
    def test_deletes_the_key(self):
        client = FakeRedis()
        asyncio.run(ingest_queue.publish_result(client, "j1", {"type": "done", "chunks": 1}))
        asyncio.run(ingest_queue.delete_results_stream(client, "j1"))
        assert ingest_queue.results_stream_key("j1") in client.deleted

    def test_never_raises_even_if_the_client_errors(self):
        class _RaisingClient(FakeRedis):
            async def delete(self, key):
                raise RuntimeError("connection reset")

        asyncio.run(ingest_queue.delete_results_stream(_RaisingClient(), "j1"))  # must not raise
