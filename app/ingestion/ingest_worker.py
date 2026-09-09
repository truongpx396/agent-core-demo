"""Redis Streams consumer — the worker half of the production ingestion
pipeline. Run one or more of these;
Redis's own consumer-group delivery guarantees each job on
`app/ingestion/ingest_queue.py::INGEST_REQUESTS_STREAM` is handed to exactly one of
them, so running more workers is the whole scaling story — same shape as
app/turns/agent_worker.py, deliberately a SEPARATE queue/consumer group from it
(see app/ingestion/ingest_queue.py's module docstring for why).

For each job: download the uploaded file from MinIO
(app/ingestion/object_store.py), dispatch to a PDF/DOCX extractor by file extension
(app/ingestion/extractors.py), and feed the resulting text into the SAME
chunk/embed/upsert pipeline every other ingest path already shares
(app/ingestion/ingestor.py::ingest_text) — this worker owns none of that logic
itself, only the download/dispatch/queue-plumbing around it.

Download and extraction are both synchronous, blocking calls (the MinIO
SDK and pypdf/python-docx are sync libraries) — called directly here
without `asyncio.to_thread`/an executor, matching this app's existing
convention of calling sync I/O (app/agent/sql_store.py's psycopg calls,
app/agent/meter.py::record_usage) directly from async contexts elsewhere; this
worker processes one job at a time regardless (`_READ_COUNT = 1`), so
there's no concurrent async task within the SAME process a blocking call
could starve. Both are fast (no per-chunk work) — nothing here needs a
progress signal.

`ingestor.ingest_text` itself is the one exception: it DOES run via
`asyncio.to_thread`, not for concurrency (still only one job at a time),
but so the event loop stays free to actually publish `on_progress`'s
"progress" events (`publish_result`, async, Redis I/O) WHILE that
synchronous embedding loop is still running in the worker thread —
calling it directly on the loop, like download/extraction above, would
mean every progress event queues up behind the whole blocking call and
all arrive at once right before "done", defeating the entire point of a
progress bar in `POST /ingest/upload`'s SSE stream (app/api/main.py).
`asyncio.run_coroutine_threadsafe` is the standard bridge from that
worker thread's sync `on_progress` callback back to publishing on this
process's actual event loop.

Run with: `python -m app.ingestion.ingest_worker` (see Makefile's `ingest-worker`
target). Needs `make up`'s Redis + MinIO running; NOT started by `make up`
itself — an opt-in path alongside `POST /ingest/upload` (app/api/main.py),
which just publishes the job and does no parsing/embedding of its own.
"""
import asyncio
import concurrent.futures
import json
import logging
import signal
import socket
import uuid
from pathlib import Path
from typing import cast

from app.core.logging_config import bind_request_id, configure_logging
from app.core.telemetry import configure_telemetry
from app.ingestion import ingestor, object_store
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX, ExtractionFailed
from app.ingestion.ingest_queue import (
    INGEST_CONSUMER_GROUP,
    INGEST_REQUESTS_STREAM,
    ensure_consumer_group,
    get_client,
    publish_result,
)
from app.turns.queue import StreamReadResponse

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_READ_COUNT = 1  # one job at a time per worker — parsing/embedding a large document can take a while
_BLOCK_MS = 5000


def _log_progress_publish_failure(job_id: str, future: concurrent.futures.Future) -> None:
    exc = future.exception()
    if exc is not None:
        logger.warning(
            "ingest_worker_progress_publish_failed",
            extra={"job_id": job_id, "error_class": type(exc).__name__},
        )


def _make_progress_reporter(client, job_id: str, loop: asyncio.AbstractEventLoop):
    """A plain SYNC callback `ingestor.ingest_text` calls directly from the
    worker thread `asyncio.to_thread` runs it on (see this module's own
    docstring for why that thread exists at all). Fire-and-forget: a
    dropped progress tick is never worth blocking embedding over, same
    "best effort" posture as `ingest_queue.delete_results_stream`'s own
    cleanup — the terminal `done`/`error` event, published normally from
    the main coroutine below, is what actually matters for correctness."""

    def on_progress(done: int, total: int) -> None:
        future = asyncio.run_coroutine_threadsafe(
            publish_result(client, job_id, {"type": "progress", "done": done, "total": total}),
            loop,
        )
        future.add_done_callback(lambda f: _log_progress_publish_failure(job_id, f))

    return on_progress


async def process_job(client, entry_id: str, fields: dict) -> None:
    """Run one ingest job and publish its outcome — always ack, even on
    failure, same "never silently redeliver an already-attempted job"
    reasoning as app/turns/agent_worker.py::process_request (a redelivered
    ingest job would re-embed and re-upsert the same document's chunks a
    second time, duplicating them in the index — not just re-run a side
    effect, but a real data-quality regression)."""
    payload = json.loads(fields["payload"])
    job_id = payload["job_id"]
    with bind_request_id(job_id):
        try:
            await publish_result(client, job_id, {"type": "started"})
            filename = payload["filename"]
            suffix = Path(filename).suffix.lower()
            extractor = EXTRACTORS_BY_SUFFIX.get(suffix)
            if extractor is None:
                raise ExtractionFailed(
                    f"unsupported file type {suffix!r} — only "
                    f"{sorted(EXTRACTORS_BY_SUFFIX)} are supported"
                )
            data = object_store.download_bytes(payload["object_key"])
            text = extractor(data)
            on_progress = _make_progress_reporter(client, job_id, asyncio.get_running_loop())
            chunks = await asyncio.to_thread(
                ingestor.ingest_text,
                text,
                title=Path(filename).stem,
                ctx=payload["ctx"],
                source=f"upload:{filename}",
                topic=payload.get("topic"),
                on_progress=on_progress,
            )
            await publish_result(client, job_id, {"type": "done", "chunks": chunks})
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            # The actual message, not just error_class — same truncated-string
            # convention as app/domains/sandbox_session.py/app/agent/skills.py.
            # Without it, this job's real failure reason only ever existed in
            # the result Redis stream (`publish_result` below), which expires
            # after RESULTS_STREAM_TTL_SECONDS — gone long before anyone
            # thinks to go looking for why a large upload silently failed.
            logger.warning(
                "ingest_worker_job_failed",
                extra={
                    "job_id": job_id,
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
            await publish_result(client, job_id, {"type": "error", "content": str(exc)})
        finally:
            await client.xack(INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)


async def run() -> None:
    client = get_client()
    await ensure_consumer_group(client)
    logger.info("ingest_worker_started", extra={"consumer": CONSUMER_NAME})

    # Graceful shutdown — same reasoning as app/turns/agent_worker.py's `run()`:
    # a SIGTERM/SIGINT stops this worker from claiming a NEW job, but never
    # interrupts one already in flight (a redelivered ingest job would
    # re-embed and re-upsert the same document's chunks a second time, a
    # real data-quality regression — see process_job's own docstring).
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    while not stop_event.is_set():
        response = cast(
            StreamReadResponse,
            await client.xreadgroup(
                INGEST_CONSUMER_GROUP,
                CONSUMER_NAME,
                {INGEST_REQUESTS_STREAM: ">"},
                count=_READ_COUNT,
                block=_BLOCK_MS,
            ),
        )
        if not response:
            continue
        _, entries = response[0]
        for entry_id, fields in entries:
            await process_job(client, entry_id, fields)

    logger.info("ingest_worker_stopping", extra={"consumer": CONSUMER_NAME})
    await client.aclose()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-ingest-worker")
    asyncio.run(run())
