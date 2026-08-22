"""Tests for app/api.py's plain, dependency-free route handlers.

Deliberately NOT using FastAPI's TestClient here: exercising the app
through it would trigger the real `lifespan` (a real durable checkpointer
file, a real Qdrant/embedding call the first time a turn runs) — this
suite calls the handler functions directly instead, the same "test the
function, not the framework wiring" approach the rest of this codebase
already takes for graph nodes (see tests/test_nodes.py's module docstring).
"""
import asyncio
import json

from app import api, queue
from app.api import ui
from app.schemas import ChatRequest
from tests.conftest import TEST_CTX
from tests.test_queue import FakeRedis


class TestUi:
    def test_returns_html_referencing_the_documented_sse_vocabulary(self):
        """The page must talk to the one published endpoint/event
        vocabulary (POST /chat/stream) — never a special-cased endpoint
        of its own (see ui()'s own docstring)."""
        html = ui()
        assert "<title>" in html
        assert "/chat/stream" in html
        for event_type in ("token", "tool_start", "tool_end", "citations", "approval_required", "error"):
            assert event_type in html

    def test_sends_the_trusted_identity_headers(self):
        """The UI must send X-Tenant-Id/X-Principal-Id itself — POST
        /chat/stream fails closed (422) without both (see app/api.py's
        get_ctx)."""
        html = ui()
        assert "X-Tenant-Id" in html
        assert "X-Principal-Id" in html


class TestChatStreamQueued:
    """POST /chat/stream/queued (GRAPH_PATTERNS.md pattern 43) — the
    producer half of the Redis Streams SSE-service/agent-worker split.
    FakeRedis (tests/test_queue.py) stands in for a real Redis Stream, and
    since nothing here ever calls astream_events_turn or touches a real
    graph, results have to be published "by hand" to simulate what a
    separate app/agent_worker.py process would otherwise do."""

    def test_publishes_a_request_then_streams_back_whatever_a_worker_publishes(
        self, monkeypatch
    ):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(message="hello", thread_id="t1")
            response = await api.chat_stream_queued(req, ctx=TEST_CTX)

            # The request was published immediately (before the response's
            # own generator has even started) — StreamingResponse's async
            # generator is lazy, so nothing has been read from the results
            # stream yet at this point.
            published = client.streams[queue.REQUESTS_STREAM]
            assert len(published) == 1
            payload = json.loads(published[0][1]["payload"])
            assert payload["text"] == "hello"
            assert payload["thread_id"] == "t1"
            assert payload["ctx"] == TEST_CTX
            request_id = payload["request_id"]

            # Simulate app/agent_worker.py publishing this turn's events.
            await queue.publish_result(client, request_id, {"type": "token", "content": "hi"})
            await queue.publish_result(client, request_id, {"type": "done"})

            chunks = [chunk async for chunk in response.body_iterator]
            return chunks, request_id

        chunks, request_id = asyncio.run(_run())

        assert any('"type": "token"' in c and '"content": "hi"' in c for c in chunks)
        assert any('"type": "done"' in c for c in chunks)

    def test_deletes_the_results_stream_once_a_terminal_event_is_seen(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(message="hi", thread_id="t2")
            response = await api.chat_stream_queued(req, ctx=TEST_CTX)
            request_id = json.loads(client.streams[queue.REQUESTS_STREAM][0][1]["payload"])[
                "request_id"
            ]
            await queue.publish_result(client, request_id, {"type": "done"})
            async for _ in response.body_iterator:
                pass
            return request_id

        request_id = asyncio.run(_run())
        assert queue.results_stream_key(request_id) in client.deleted

    def test_publishes_attached_images_onto_the_request(self, monkeypatch):
        """GRAPH_PATTERNS.md pattern 44 — images ride the same request
        payload all the way to app/agent_worker.py."""
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(
                message="what is this?", thread_id="t3", images=["https://example.com/cat.png"]
            )
            response = await api.chat_stream_queued(req, ctx=TEST_CTX)
            payload = json.loads(client.streams[queue.REQUESTS_STREAM][0][1]["payload"])
            request_id = payload["request_id"]
            await queue.publish_result(client, request_id, {"type": "done"})
            async for _ in response.body_iterator:
                pass
            return payload

        payload = asyncio.run(_run())
        assert payload["images"] == ["https://example.com/cat.png"]
