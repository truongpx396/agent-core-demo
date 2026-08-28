"""Tests for app/ingestion/ingest_worker.py's process_job — the production
ingestion pipeline's queue consumer. object_store.download_bytes and the
extractors are monkeypatched so these never touch a real MinIO/PDF/DOCX;
ingestor.ingest_text is monkeypatched too, so these test the
download -> extract -> ingest WIRING, not any of those three pieces'
own logic (each already has its own dedicated tests).
"""
import asyncio
import json

from app.ingestion import ingest_queue, ingest_worker
from tests.turns.test_queue import FakeRedis

TEST_CTX = {"tenant": "acme", "principal": "p1", "claims": {}}


def _entry(job_id="j1", filename="report.pdf", object_key="acme/abc-report.pdf", topic=None, ctx=None):
    payload = json.dumps(
        {
            "job_id": job_id,
            "object_key": object_key,
            "filename": filename,
            "content_type": "application/pdf",
            "ctx": ctx or TEST_CTX,
            "topic": topic,
        }
    )
    return "1-0", {"payload": payload}


class TestProcessJob:
    def test_happy_path_downloads_extracts_ingests_and_publishes_done(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(
            ingest_worker.object_store, "download_bytes", lambda key: (captured.setdefault("key", key), b"pdf-bytes")[1]
        )

        def fake_extract_pdf(data):
            captured["extracted_from"] = data
            return "Refund policy: 30 days."

        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", fake_extract_pdf)

        def fake_ingest_text(text, title, ctx, source, topic=None):
            captured.update(text=text, title=title, ctx=ctx, source=source, topic=topic)
            return 3

        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", fake_ingest_text)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j1", filename="report.pdf", topic="company")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        assert captured["key"] == "acme/abc-report.pdf"
        assert captured["extracted_from"] == b"pdf-bytes"
        assert captured["text"] == "Refund policy: 30 days."
        assert captured["title"] == "report"
        assert captured["source"] == "upload:report.pdf"
        assert captured["topic"] == "company"
        assert captured["ctx"] == TEST_CTX

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j1")]]
        assert events == [{"type": "started"}, {"type": "done", "chunks": 3}]
        assert client.acked == [entry_id]

    def test_docx_dispatches_to_the_docx_extractor(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"docx-bytes")
        monkeypatch.setitem(
            ingest_worker.EXTRACTORS_BY_SUFFIX, ".docx", lambda data: captured.setdefault("called", True) or "text"
        )
        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", lambda *a, **kw: 1)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j2", filename="notes.docx", object_key="acme/xyz-notes.docx")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        assert captured.get("called") is True

    def test_unsupported_file_type_publishes_an_error_and_acks(self, monkeypatch):
        client = FakeRedis()
        entry_id, fields = _entry(job_id="j3", filename="spreadsheet.xlsx", object_key="k")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j3")]]
        assert events[0] == {"type": "started"}
        assert events[1]["type"] == "error"
        assert ".xlsx" in events[1]["content"]
        assert client.acked == [entry_id]

    def test_a_download_failure_publishes_an_error_and_still_acks(self, monkeypatch):
        def failing_download(key):
            raise RuntimeError("MinIO unreachable")

        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", failing_download)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j4")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j4")]]
        assert events[-1] == {"type": "error", "content": "MinIO unreachable"}
        assert client.acked == [entry_id]

    def test_an_extraction_failure_publishes_an_error_and_still_acks(self, monkeypatch):
        from app.ingestion.extractors import ExtractionFailed

        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"garbage")

        def failing_extract(data):
            raise ExtractionFailed("could not parse PDF: bad xref")

        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", failing_extract)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j5")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j5")]]
        assert events[-1]["type"] == "error"
        assert "could not parse PDF" in events[-1]["content"]
        assert client.acked == [entry_id]

    def test_an_ingest_refusal_publishes_an_error_and_still_acks(self, monkeypatch):
        """ingest_text itself refuses (e.g. an invalid ctx, though that
        shouldn't happen given this worker always forwards a real one) —
        the SAME "report as an error, still ack" contract applies
        regardless of which stage in the pipeline actually failed."""
        from app.ingestion.ingestor import IngestRefused

        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"pdf-bytes")
        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", lambda data: "some text")

        def refusing_ingest(*a, **kw):
            raise IngestRefused("a valid tenant+principal ctx is required to ingest content")

        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", refusing_ingest)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j6")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j6")]]
        assert events[-1]["type"] == "error"
        assert client.acked == [entry_id]


class TestRunLoop:
    def test_processes_one_job_end_to_end_via_the_consumer_group(self, monkeypatch):
        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"pdf-bytes")
        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", lambda data: "text")
        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", lambda *a, **kw: 5)
        client = FakeRedis()
        monkeypatch.setattr(ingest_worker, "get_client", lambda: client)

        async def _run_one_iteration():
            await ingest_queue.ensure_consumer_group(client)
            entry_id, fields = _entry(job_id="j7")
            client.streams[ingest_queue.INGEST_REQUESTS_STREAM].append((entry_id, fields))
            response = await client.xreadgroup(
                ingest_queue.INGEST_CONSUMER_GROUP,
                ingest_worker.CONSUMER_NAME,
                {ingest_queue.INGEST_REQUESTS_STREAM: ">"},
                count=1,
            )
            _, entries = response[0]
            for eid, f in entries:
                await ingest_worker.process_job(client, eid, f)

        asyncio.run(_run_one_iteration())

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j7")]]
        assert events == [{"type": "started"}, {"type": "done", "chunks": 5}]
