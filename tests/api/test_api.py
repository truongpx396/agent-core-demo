"""Tests for app/api/main.py's plain, dependency-free route handlers.

Deliberately NOT using FastAPI's TestClient here: exercising the app
through it would trigger the real `lifespan` (a real durable checkpointer
file, a real Qdrant/embedding call the first time a turn runs) — this
suite calls the handler functions directly instead, the same "test the
function, not the framework wiring" approach the rest of this codebase
already takes for graph nodes (see tests/agent/test_nodes.py's module docstring).
"""
import asyncio
import io
import json

import pytest
from fastapi import HTTPException, Response

from app.agent import sessions
from app.api import main as api
from app.api.main import ui
from app.api.schemas import CancelRequest, ChatRequest, ResumeRequest
from app.ingestion import ingest_queue
from app.turns import queue
from tests.conftest import TEST_CTX
from tests.turns.test_queue import FakeRedis


class TestHealthReady:
    """GET /health/ready — see app/api/health.py's own tests for
    check_dependencies() itself; this just checks the HTTP-shape mapping
    (status code + body) on top of it."""

    def test_200_and_ready_when_every_dependency_is_up(self, monkeypatch):
        async def fake_check_dependencies():
            return {"qdrant": True, "appdata_postgres": True, "checkpointer_postgres": True, "redis": True}

        monkeypatch.setattr(api.health_checks, "check_dependencies", fake_check_dependencies)
        response = Response()

        result = asyncio.run(api.health_ready(response))

        assert response.status_code == 200
        assert result.status == "ready"
        assert result.checks["qdrant"] is True

    def test_503_and_degraded_when_any_dependency_is_down(self, monkeypatch):
        async def fake_check_dependencies():
            return {"qdrant": False, "appdata_postgres": True, "checkpointer_postgres": True, "redis": True}

        monkeypatch.setattr(api.health_checks, "check_dependencies", fake_check_dependencies)
        response = Response()

        result = asyncio.run(api.health_ready(response))

        assert response.status_code == 503
        assert result.status == "degraded"
        assert result.checks["qdrant"] is False


class TestUsage:
    """GET /usage — a thin pass-through to app/agent/meter.py::usage_summary,
    called once all-time and once scoped to the rolling 24h window
    app/agent/runtime.py::_tenant_over_daily_budget itself checks."""

    def test_reports_all_time_and_rolling_24h_figures(self, monkeypatch):
        calls = []

        def fake_usage_summary(tenant, principal=None, since=None):
            calls.append({"tenant": tenant, "since": since})
            if since is None:
                return {"total_tokens": 5000, "total_cost_usd": 3.5}
            return {"total_tokens": 200, "total_cost_usd": 0.1}

        monkeypatch.setattr(api.meter, "usage_summary", fake_usage_summary)
        monkeypatch.setattr(api, "MAX_COST_USD_PER_TENANT_PER_DAY", 20.0)

        result = api.usage(ctx=TEST_CTX)

        assert result.total_tokens == 5000
        assert result.total_cost_usd == 3.5
        assert result.last_24h_cost_usd == 0.1
        assert result.daily_budget_usd == 20.0
        assert len(calls) == 2
        assert calls[0]["tenant"] == TEST_CTX["tenant"]
        assert calls[1]["since"] is not None


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
        /chat/stream fails closed (422) without both (see app/api/main.py's
        get_ctx)."""
        html = ui()
        assert "X-Tenant-Id" in html
        assert "X-Principal-Id" in html

    def test_sends_a_domain_selector_for_the_queued_endpoints(self):
        """The page must send X-Domain on the queued path — see
        app/api/main.py's get_domain, which the three queued endpoints
        (chat_stream_queued/chat_resume/chat_cancel) depend on."""
        html = ui()
        assert "X-Domain" in html
        assert 'id="domainSelect"' in html


class TestGetDomain:
    """app/api/main.py::get_domain's own runtime branch: an unknown domain
    name must fail loud, the same discipline
    app/domains/registry.py::resolve_domain already applies at process
    start, just surfaced as a 422 here since this is a per-request value
    rather than a per-process one. `Header("acme")`'s default-when-absent
    behavior is FastAPI's own wiring, not this function's logic, so — like
    get_ctx's required-header behavior above — it's out of scope for this
    suite's "call the handler directly" style (see this module's own
    docstring)."""

    def test_passes_through_a_known_domain(self):
        assert asyncio.run(api.get_domain(x_domain="support")) == "support"

    def test_rejects_an_unknown_domain_with_422(self):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.get_domain(x_domain="not-a-real-domain"))
        assert exc_info.value.status_code == 422
        assert "not-a-real-domain" in exc_info.value.detail


class TestChatStreamQueued:
    """POST /chat/stream/queued (GRAPH_PATTERNS.md pattern 43) — the
    producer half of the Redis Streams SSE-service/agent-worker split.
    FakeRedis (tests/turns/test_queue.py) stands in for a real Redis Stream, and
    since nothing here ever calls astream_events_turn or touches a real
    graph, results have to be published "by hand" to simulate what a
    separate app/turns/agent_worker.py process would otherwise do."""

    def test_publishes_a_request_then_streams_back_whatever_a_worker_publishes(
        self, monkeypatch
    ):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(message="hello", thread_id="t1")
            response = await api.chat_stream_queued(req, ctx=TEST_CTX, domain="acme")

            # The request was published immediately (before the response's
            # own generator has even started) — StreamingResponse's async
            # generator is lazy, so nothing has been read from the results
            # stream yet at this point.
            published = client.streams[queue.requests_stream_key("acme")]
            assert len(published) == 1
            payload = json.loads(published[0][1]["payload"])
            assert payload["text"] == "hello"
            assert payload["thread_id"] == "t1"
            assert payload["ctx"] == TEST_CTX
            request_id = payload["request_id"]

            # Simulate app/turns/agent_worker.py publishing this turn's events.
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
            response = await api.chat_stream_queued(req, ctx=TEST_CTX, domain="acme")
            request_id = json.loads(client.streams[queue.requests_stream_key("acme")][0][1]["payload"])[
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
        payload all the way to app/turns/agent_worker.py."""
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(
                message="what is this?", thread_id="t3", images=["https://example.com/cat.png"]
            )
            response = await api.chat_stream_queued(req, ctx=TEST_CTX, domain="acme")
            payload = json.loads(client.streams[queue.requests_stream_key("acme")][0][1]["payload"])
            request_id = payload["request_id"]
            await queue.publish_result(client, request_id, {"type": "done"})
            async for _ in response.body_iterator:
                pass
            return payload

        payload = asyncio.run(_run())
        assert payload["images"] == ["https://example.com/cat.png"]

    def test_a_non_acme_domain_publishes_onto_its_own_stream(self, monkeypatch):
        """The whole point of threading `domain` through — an X-Domain:
        support turn must land where ONLY an `AGENT_DOMAIN=support`
        app/turns/agent_worker.py pool is listening, never on Acme's own
        stream (see app/turns/queue.py::publish_request's own docstring)."""
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ChatRequest(message="where is my order?", thread_id="t4")
            await api.chat_stream_queued(req, ctx=TEST_CTX, domain="support")

        asyncio.run(_run())
        assert queue.requests_stream_key("acme") not in client.streams
        published = client.streams[queue.requests_stream_key("support")]
        assert len(published) == 1


class TestChatResume:
    """POST /chat/resume — publishes a `"resume"` job onto the SAME
    Redis Stream a new turn uses (app/turns/queue.py::publish_resume_request),
    not a separate queue; app/turns/agent_worker.py dispatches on
    `payload["kind"]`."""

    def test_publishes_a_resume_job_with_approved_and_thread_id(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = ResumeRequest(thread_id="t1", approved=True)
            response = await api.chat_resume(req, ctx=TEST_CTX, domain="acme")

            published = client.streams[queue.requests_stream_key("acme")]
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
            await api.chat_resume(req, ctx=TEST_CTX, domain="acme")
            return json.loads(client.streams[queue.requests_stream_key("acme")][0][1]["payload"])

        payload = asyncio.run(_run())
        assert payload["approved"] is False


class TestChatCancel:
    """POST /chat/cancel — two independent mechanisms fired unconditionally
    (app/api/main.py's own docstring): a Redis cancel-flag AND a `"cancel"` job
    published onto the same queue."""

    def test_sets_the_cancel_flag_and_publishes_a_cancel_job(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        async def _run():
            req = CancelRequest(thread_id="t1")
            response = await api.chat_cancel(req, ctx=TEST_CTX, domain="acme")

            assert await queue.is_cancelled(client, "t1") is True

            published = client.streams[queue.requests_stream_key("acme")]
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
    """GET /chat/sessions — a thin pass-through to app/agent/sessions.py::list_sessions,
    scoped by whatever ctx get_ctx resolved (not a caller-supplied field)
    AND by the X-Domain-derived `domain` (GRAPH_PATTERNS.md pattern 49)."""

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

        def fake_list_sessions(ctx, domain):
            captured["ctx"] = ctx
            captured["domain"] = domain
            return rows

        monkeypatch.setattr(sessions, "list_sessions", fake_list_sessions)

        result = api.chat_sessions(ctx=TEST_CTX, domain="acme")

        # Calling the handler directly (this file's established
        # convention) bypasses FastAPI's response_model coercion — that
        # only happens through the real ASGI request/response cycle, so
        # this is the raw list[dict] list_sessions itself returned.
        assert captured["ctx"] == TEST_CTX
        assert captured["domain"] == "acme"
        assert [r["thread_id"] for r in result] == ["t1"]
        assert result[0]["title"] == "Refund question"

    def test_a_different_domain_is_passed_through_too(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            sessions, "list_sessions", lambda ctx, domain: captured.setdefault("domain", domain) or []
        )

        api.chat_sessions(ctx=TEST_CTX, domain="support")

        assert captured["domain"] == "support"


class TestChatSessionMessages:
    """GET /chat/sessions/{thread_id}/messages — session_belongs_to is the
    ENTIRE authorization boundary (the shared checkpointer
    get_session_messages reads has no tenant/principal/domain of its own)."""

    def test_returns_the_transcript_when_owned(self, monkeypatch):
        monkeypatch.setattr(sessions, "session_belongs_to", lambda ctx, thread_id, domain: True)

        async def fake_get_session_messages(thread_id):
            assert thread_id == "t1"
            return [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]

        monkeypatch.setattr(api, "get_session_messages", fake_get_session_messages)

        result = asyncio.run(api.chat_session_messages("t1", ctx=TEST_CTX, domain="acme"))

        # Same note as TestChatSessions above — raw dicts, not
        # response_model-coerced Pydantic objects, when called directly.
        assert [m["role"] for m in result] == ["user", "assistant"]
        assert result[0]["text"] == "hi"

    def test_404s_when_not_owned_never_reading_the_transcript(self, monkeypatch):
        import pytest
        from fastapi import HTTPException

        monkeypatch.setattr(sessions, "session_belongs_to", lambda ctx, thread_id, domain: False)

        async def fail_if_called(thread_id):
            raise AssertionError("get_session_messages should not be called")

        monkeypatch.setattr(api, "get_session_messages", fail_if_called)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.chat_session_messages("someone-elses-thread", ctx=TEST_CTX, domain="acme"))

        assert exc_info.value.status_code == 404

    def test_404s_when_owned_by_a_different_domain(self, monkeypatch):
        """The exact scenario this pattern exists to prevent: a thread
        opened under "support" must not be readable while "sales" is the
        selected domain, even for its own owner."""
        import pytest
        from fastapi import HTTPException

        captured = {}

        def fake_session_belongs_to(ctx, thread_id, domain):
            captured["domain"] = domain
            return False

        monkeypatch.setattr(sessions, "session_belongs_to", fake_session_belongs_to)

        async def fail_if_called(thread_id):
            raise AssertionError("get_session_messages should not be called")

        monkeypatch.setattr(api, "get_session_messages", fail_if_called)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.chat_session_messages("t1", ctx=TEST_CTX, domain="sales"))

        assert exc_info.value.status_code == 404
        assert captured["domain"] == "sales"


def _upload_file(filename, data, content_type):
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    return UploadFile(
        file=io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type})
    )


class TestIngestUpload:
    """POST /ingest/upload — MinIO upload + a job published onto
    app/ingestion/ingest_queue.py's SEPARATE queue from chat turns. Both
    object_store.upload_bytes and ingest_queue.publish_ingest_request are
    monkeypatched, matching this file's "test the function, not live
    services" convention."""

    def test_uploads_to_minio_and_publishes_a_job_per_file(self, monkeypatch):
        uploaded = []
        published = []

        monkeypatch.setattr(
            api.object_store,
            "upload_bytes",
            lambda key, data, content_type: uploaded.append((key, data, content_type)),
        )

        async def fake_publish(client, *, job_id, object_key, filename, content_type, ctx, topic=None):
            published.append(
                {
                    "job_id": job_id,
                    "object_key": object_key,
                    "filename": filename,
                    "content_type": content_type,
                    "ctx": ctx,
                    "topic": topic,
                }
            )

        monkeypatch.setattr(api.ingest_queue, "publish_ingest_request", fake_publish)
        client = FakeRedis()
        monkeypatch.setattr(queue, "get_client", lambda: client)

        files = [_upload_file("report.pdf", b"pdf-bytes", "application/pdf")]

        result = asyncio.run(api.ingest_upload(files=files, topic="company", ctx=TEST_CTX))

        assert len(uploaded) == 1
        key, data, content_type = uploaded[0]
        assert key.startswith(f"{TEST_CTX['tenant']}/")
        assert key.endswith("-report.pdf")
        assert data == b"pdf-bytes"
        assert content_type == "application/pdf"

        assert len(published) == 1
        assert published[0]["filename"] == "report.pdf"
        assert published[0]["topic"] == "company"
        assert published[0]["ctx"] == TEST_CTX
        assert published[0]["object_key"] == key

        assert len(result) == 1
        assert result[0].filename == "report.pdf"
        assert result[0].job_id == published[0]["job_id"]

    def test_multiple_files_each_get_their_own_job(self, monkeypatch):
        monkeypatch.setattr(api.object_store, "upload_bytes", lambda *a, **kw: None)

        async def fake_publish(client, **kw):
            pass

        monkeypatch.setattr(api.ingest_queue, "publish_ingest_request", fake_publish)
        monkeypatch.setattr(queue, "get_client", lambda: FakeRedis())

        files = [
            _upload_file("report.pdf", b"a", "application/pdf"),
            _upload_file(
                "notes.docx",
                b"b",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ]

        result = asyncio.run(api.ingest_upload(files=files, topic=None, ctx=TEST_CTX))

        assert [r.filename for r in result] == ["report.pdf", "notes.docx"]
        assert result[0].job_id != result[1].job_id

    def test_an_unsupported_extension_is_rejected_before_any_upload(self, monkeypatch):
        uploaded = []
        monkeypatch.setattr(api.object_store, "upload_bytes", lambda *a, **kw: uploaded.append(a))

        files = [_upload_file("spreadsheet.xlsx", b"data", "application/vnd.ms-excel")]

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.ingest_upload(files=files, topic=None, ctx=TEST_CTX))

        assert exc_info.value.status_code == 400
        assert uploaded == []  # never reached MinIO

    def test_a_path_component_in_the_filename_is_stripped(self, monkeypatch):
        """A client-supplied filename is untrusted input — the object key
        it feeds into must never carry a caller-controlled directory
        component, even though S3-style object keys aren't literal
        filesystem paths (no real traversal risk), just for a sane,
        predictable key shape."""
        uploaded = []
        monkeypatch.setattr(api.object_store, "upload_bytes", lambda key, *a, **kw: uploaded.append(key))

        async def fake_publish(client, **kw):
            pass

        monkeypatch.setattr(api.ingest_queue, "publish_ingest_request", fake_publish)
        monkeypatch.setattr(queue, "get_client", lambda: FakeRedis())

        files = [_upload_file("../../etc/passwd.pdf", b"x", "application/pdf")]
        asyncio.run(api.ingest_upload(files=files, topic=None, ctx=TEST_CTX))

        assert "../" not in uploaded[0]
        assert uploaded[0].endswith("-passwd.pdf")

    def test_a_file_over_the_size_cap_is_rejected_before_any_upload(self, monkeypatch):
        """_read_bounded (app/api/main.py) checks the running total WHILE
        reading, not after — this only has to prove the outcome (413,
        never reaches MinIO), not the memory-bounding mechanism itself."""
        monkeypatch.setattr(api, "_MAX_UPLOAD_BYTES", 10)  # tiny, so the test payload need not be huge
        monkeypatch.setattr(api, "_UPLOAD_READ_CHUNK_BYTES", 4)
        uploaded = []
        monkeypatch.setattr(api.object_store, "upload_bytes", lambda *a, **kw: uploaded.append(a))

        files = [_upload_file("report.pdf", b"x" * 100, "application/pdf")]

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api.ingest_upload(files=files, topic=None, ctx=TEST_CTX))

        assert exc_info.value.status_code == 413
        assert uploaded == []  # never reached MinIO

    def test_a_file_within_the_size_cap_still_uploads(self, monkeypatch):
        monkeypatch.setattr(api, "_MAX_UPLOAD_BYTES", 1000)
        uploaded = []
        monkeypatch.setattr(
            api.object_store, "upload_bytes", lambda key, data, ct: uploaded.append(data)
        )

        async def fake_publish(client, **kw):
            pass

        monkeypatch.setattr(api.ingest_queue, "publish_ingest_request", fake_publish)
        monkeypatch.setattr(queue, "get_client", lambda: FakeRedis())

        files = [_upload_file("report.pdf", b"x" * 100, "application/pdf")]
        asyncio.run(api.ingest_upload(files=files, topic=None, ctx=TEST_CTX))

        assert uploaded == [b"x" * 100]


class TestIngestStream:
    def test_streams_back_whatever_a_worker_publishes(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(ingest_queue, "get_client", lambda: client)

        async def _run():
            response = await api.ingest_stream("j1")
            await ingest_queue.publish_result(client, "j1", {"type": "started"})
            await ingest_queue.publish_result(client, "j1", {"type": "done", "chunks": 4})
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(_run())
        assert any('"type": "started"' in c for c in chunks)
        assert any('"type": "done"' in c and '"chunks": 4' in c for c in chunks)

    def test_deletes_the_results_stream_once_terminal(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(ingest_queue, "get_client", lambda: client)

        async def _run():
            response = await api.ingest_stream("j2")
            await ingest_queue.publish_result(client, "j2", {"type": "error", "content": "bad file"})
            async for _ in response.body_iterator:
                pass

        asyncio.run(_run())
        assert ingest_queue.results_stream_key("j2") in client.deleted
