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

from app import api, queue, sessions
from app.api import ui
from app.schemas import CancelRequest, ChatRequest, ResumeRequest
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


class TestChatResume:
    """POST /chat/resume — publishes a `"resume"` job onto the SAME
    Redis Stream a new turn uses (app/queue.py::publish_resume_request),
    not a separate queue; app/agent_worker.py dispatches on
    `payload["kind"]`."""

    def test_publishes_a_resume_job_with_approved_and_thread_id(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ResumeRequest(thread_id="t1", approved=True)
            response = await api.chat_resume(req, ctx=TEST_CTX)

            published = client.streams[queue.REQUESTS_STREAM]
            assert len(published) == 1
            payload = json.loads(published[0][1]["payload"])
            assert payload["kind"] == "resume"
            assert payload["thread_id"] == "t1"
            assert payload["approved"] is True
            assert payload["ctx"] == TEST_CTX
            request_id = payload["request_id"]

            await queue.publish_result(client, request_id, {"type": "done"})
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(_run())
        assert any('"type": "done"' in c for c in chunks)

    def test_a_rejection_carries_approved_false(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ResumeRequest(thread_id="t2", approved=False)
            await api.chat_resume(req, ctx=TEST_CTX)
            return json.loads(client.streams[queue.REQUESTS_STREAM][0][1]["payload"])

        payload = asyncio.run(_run())
        assert payload["approved"] is False


class TestChatCancel:
    """POST /chat/cancel — two independent mechanisms fired unconditionally
    (app/api.py's own docstring): a Redis cancel-flag AND a `"cancel"` job
    published onto the same queue."""

    def test_sets_the_cancel_flag_and_publishes_a_cancel_job(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = CancelRequest(thread_id="t1")
            response = await api.chat_cancel(req, ctx=TEST_CTX)

            assert await queue.is_cancelled(client, "t1") is True

            published = client.streams[queue.REQUESTS_STREAM]
            assert len(published) == 1
            payload = json.loads(published[0][1]["payload"])
            assert payload["kind"] == "cancel"
            assert payload["thread_id"] == "t1"
            assert payload["ctx"] == TEST_CTX
            request_id = payload["request_id"]

            await queue.publish_result(client, request_id, {"type": "done"})
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(_run())
        assert any('"type": "done"' in c for c in chunks)


class TestChatSessions:
    """GET /chat/sessions — a thin pass-through to app/sessions.py::list_sessions,
    scoped by whatever ctx get_ctx resolved (not a caller-supplied field)."""

    def test_returns_whatever_list_sessions_reports(self, monkeypatch):
        rows = [
            {
                "thread_id": "t1",
                "title": "Refund question",
                "created_at": "2026-01-01T00:00:00Z",
                "last_active_at": "2026-01-02T00:00:00Z",
            }
        ]
        captured = {}

        def fake_list_sessions(ctx):
            captured["ctx"] = ctx
            return rows

        monkeypatch.setattr(sessions, "list_sessions", fake_list_sessions)

        result = api.chat_sessions(ctx=TEST_CTX)

        # Calling the handler directly (this file's established
        # convention) bypasses FastAPI's response_model coercion — that
        # only happens through the real ASGI request/response cycle, so
        # this is the raw list[dict] list_sessions itself returned.
        assert captured["ctx"] == TEST_CTX
        assert [r["thread_id"] for r in result] == ["t1"]
        assert result[0]["title"] == "Refund question"


class TestChatSessionMessages:
    """GET /chat/sessions/{thread_id}/messages — session_belongs_to is the
    ENTIRE authorization boundary (the shared checkpointer
    get_session_messages reads has no tenant/principal of its own)."""

    def test_returns_the_transcript_when_owned(self, monkeypatch):
        monkeypatch.setattr(sessions, "session_belongs_to", lambda ctx, thread_id: True)

        async def fake_get_session_messages(thread_id):
            assert thread_id == "t1"
            return [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]

        monkeypatch.setattr(api, "get_session_messages", fake_get_session_messages)

        result = asyncio.run(api.chat_session_messages("t1", ctx=TEST_CTX))

        # Same note as TestChatSessions above — raw dicts, not
        # response_model-coerced Pydantic objects, when called directly.
        assert [m["role"] for m in result] == ["user", "assistant"]
        assert result[0]["text"] == "hi"

    def test_404s_when_not_owned_never_reading_the_transcript(self, monkeypatch):
        import pytest
        from fastapi import HTTPException

        monkeypatch.setattr(sessions, "session_belongs_to", lambda ctx, thread_id: False)

        async def fail_if_called(thread_id):
            raise AssertionError("get_session_messages should not be called")

        monkeypatch.setattr(api, "get_session_messages", fail_if_called)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.chat_session_messages("someone-elses-thread", ctx=TEST_CTX))

        assert exc_info.value.status_code == 404
