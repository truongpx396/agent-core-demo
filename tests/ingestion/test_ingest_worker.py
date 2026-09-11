"""Tests for app/ingestion/ingest_worker.py's process_job — the production
ingestion pipeline's queue consumer. object_store.download_bytes and the
extractors are monkeypatched so these never touch a real MinIO/PDF/DOCX;
ingestor.ingest_text is monkeypatched too, so these test the
download -> extract -> ingest WIRING, not any of those three pieces'
own logic (each already has its own dedicated tests).
"""
import asyncio
import contextlib
import json
import threading
import time

from app.ingestion import ingest_queue, ingest_worker
from tests.turns.test_queue import FakeRedis

TEST_CTX = {"tenant": "ecorp", "principal": "p1", "claims": {}}


def _entry(job_id="j1", filename="report.pdf", object_key="ecorp/abc-report.pdf", topic=None, ctx=None):
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

        def fake_ingest_text(text, title, ctx, source, topic=None, on_progress=None):
            captured.update(text=text, title=title, ctx=ctx, source=source, topic=topic)
            return 3

        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", fake_ingest_text)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j1", filename="report.pdf", topic="company")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        assert captured["key"] == "ecorp/abc-report.pdf"
        assert captured["extracted_from"] == b"pdf-bytes"
        assert captured["text"] == "Refund policy: 30 days."
        assert captured["title"] == "report"
        assert captured["source"] == "upload:report.pdf"
        assert captured["topic"] == "company"
        assert captured["ctx"] == TEST_CTX

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j1")]]
        assert events == [{"type": "started"}, {"type": "done", "chunks": 3}]
        assert client.acked == [entry_id]

    def test_ingest_texts_progress_callback_publishes_progress_events_in_order(self, monkeypatch):
        """The actual point of running ingest_text via asyncio.to_thread —
        proves progress events reach the results stream WHILE ingest_text
        is still "running" (simulated here by calling on_progress twice
        before returning), in order, before the terminal `done` event, not
        collected and only visible afterward."""
        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"pdf-bytes")
        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", lambda data: "text")

        def fake_ingest_text(text, title, ctx, source, topic=None, on_progress=None):
            on_progress(200, 570)
            on_progress(570, 570)
            return 570

        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", fake_ingest_text)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j8")

        asyncio.run(ingest_worker.process_job(client, entry_id, fields))

        events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key("j8")]]
        assert events == [
            {"type": "started"},
            {"type": "progress", "done": 200, "total": 570},
            {"type": "progress", "done": 570, "total": 570},
            {"type": "done", "chunks": 570},
        ]

    def test_docx_dispatches_to_the_docx_extractor(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", lambda key: b"docx-bytes")
        monkeypatch.setitem(
            ingest_worker.EXTRACTORS_BY_SUFFIX, ".docx", lambda data: captured.setdefault("called", True) or "text"
        )
        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", lambda *a, **kw: 1)

        client = FakeRedis()
        entry_id, fields = _entry(job_id="j2", filename="notes.docx", object_key="ecorp/xyz-notes.docx")

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

    def test_sizes_the_default_executor_to_max_concurrency(self, monkeypatch):
        """The loop's default executor (what asyncio.to_thread borrows from)
        has no relationship to _MAX_CONCURRENCY out of the box — Python's own
        default is min(32, cpu_count+4), unrelated to this app's own
        concurrency setting and hard-capped at 32 regardless of host (see
        run()'s own comment). Proves run() sizes it explicitly instead,
        rather than leaving it to that unrelated, environment-dependent
        default."""
        client = FakeRedis()
        monkeypatch.setattr(ingest_worker, "get_client", lambda: client)

        # FakeRedis.xreadgroup has no internal `await` — calling it never
        # actually suspends the coroutine, so run()'s `while` loop would
        # busy-spin forever without ever yielding back to the event loop
        # (starving even the asyncio.sleep(0.05)/task.cancel() below). A
        # real await point here is what lets this test's own cancellation
        # actually take effect.
        real_xreadgroup = client.xreadgroup

        async def yielding_xreadgroup(*a, **kw):
            await asyncio.sleep(0)
            return await real_xreadgroup(*a, **kw)

        client.xreadgroup = yielding_xreadgroup

        captured = {}

        async def _run_briefly():
            # Patched on the LOOP INSTANCE, not the class — set_default_executor
            # is implemented on BaseEventLoop (concrete), not AbstractEventLoop,
            # so a class-level patch on the abstract base is silently never hit.
            loop = asyncio.get_running_loop()
            real_set_default_executor = loop.set_default_executor

            def spy_set_default_executor(executor):
                captured["max_workers"] = executor._max_workers
                return real_set_default_executor(executor)

            loop.set_default_executor = spy_set_default_executor

            task = asyncio.create_task(ingest_worker.run())
            await asyncio.sleep(0.05)  # let run() get past executor setup, into its read loop
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_run_briefly())

        assert captured["max_workers"] == ingest_worker._MAX_CONCURRENCY


class TestConcurrentDispatch:
    """run() no longer awaits process_job one job at a time — it acquires a
    semaphore slot, then asyncio.create_tasks _process_with_limit per entry
    (see run()'s own comments for why the semaphore is acquired BEFORE task
    creation, not inside it). Same shape, same test structure, as
    app/turns/agent_worker.py::TestConcurrentDispatch — this replicates that
    exact acquire-then-dispatch pattern against several ingest jobs at once."""

    def test_bounds_concurrency_and_actually_overlaps(self, monkeypatch):
        max_concurrency = 2
        num_jobs = 5
        current = 0
        peak = 0
        count_lock = threading.Lock()

        def slow_download(key):
            # object_store.download_bytes is sync and runs via
            # asyncio.to_thread inside process_job, i.e. on a REAL OS thread
            # from the default executor pool — plain threading primitives
            # (not asyncio ones, which aren't safe to share across the
            # separate thread asyncio.to_thread hands this to) are what
            # actually measure overlap here.
            nonlocal current, peak
            with count_lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.05)  # long enough for siblings to overlap, short enough for a fast test
            with count_lock:
                current -= 1
            return b"pdf-bytes"

        monkeypatch.setattr(ingest_worker.object_store, "download_bytes", slow_download)
        monkeypatch.setitem(ingest_worker.EXTRACTORS_BY_SUFFIX, ".pdf", lambda data: "text")
        monkeypatch.setattr(ingest_worker.ingestor, "ingest_text", lambda *a, **kw: 1)

        client = FakeRedis()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _dispatch_all():
            tasks = []
            for i in range(num_jobs):
                entry_id, fields = _entry(job_id=f"j{i}")
                await semaphore.acquire()
                task = asyncio.create_task(
                    ingest_worker._process_with_limit(client, entry_id, fields, semaphore)
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
            events = [json.loads(f["payload"]) for _, f in client.streams[ingest_queue.results_stream_key(f"j{i}")]]
            assert events == [{"type": "started"}, {"type": "done", "chunks": 1}]
