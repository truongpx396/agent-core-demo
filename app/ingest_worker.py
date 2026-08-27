"""Redis Streams consumer — the worker half of the production ingestion
pipeline. Run one or more of these;
Redis's own consumer-group delivery guarantees each job on
`app/ingest_queue.py::INGEST_REQUESTS_STREAM` is handed to exactly one of
them, so running more workers is the whole scaling story — same shape as
app/agent_worker.py, deliberately a SEPARATE queue/consumer group from it
(see app/ingest_queue.py's module docstring for why).

For each job: download the uploaded file from MinIO
(app/object_store.py), dispatch to a PDF/DOCX extractor by file extension
(app/extractors.py), and feed the resulting text into the SAME
chunk/embed/upsert pipeline every other ingest path already shares
(app/ingestor.py::ingest_text) — this worker owns none of that logic
itself, only the download/dispatch/queue-plumbing around it.

Download and extraction are both synchronous, blocking calls (the MinIO
SDK and pypdf/python-docx are sync libraries) — called directly here
without `asyncio.to_thread`/an executor, matching this app's existing
convention of calling sync I/O (app/sql_store.py's psycopg calls,
app/meter.py::record_usage) directly from async contexts elsewhere; this
worker processes one job at a time regardless (`_READ_COUNT = 1`), so
there's no concurrent async task within the SAME process a blocking call
could starve.

Run with: `python -m app.ingest_worker` (see Makefile's `ingest-worker`
target). Needs `make up`'s Redis + MinIO running; NOT started by `make up`
itself — an opt-in path alongside `POST /ingest/upload` (app/api.py),
which just publishes the job and does no parsing/embedding of its own.
"""
import asyncio
import json
import logging
import signal
import socket
import uuid
from pathlib import Path
from typing import cast

from app import ingestor, object_store
from app.extractors import EXTRACTORS_BY_SUFFIX, ExtractionFailed
from app.ingest_queue import (
    INGEST_CONSUMER_GROUP,
    INGEST_REQUESTS_STREAM,
    ensure_consumer_group,
    get_client,
    publish_result,
)
from app.logging_config import bind_request_id, configure_logging
from app.queue import StreamReadResponse

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_READ_COUNT = 1  # one job at a time per worker — parsing/embedding a large document can take a while
_BLOCK_MS = 5000


async def process_job(client, entry_id: str, fields: dict) -> None:
    """Run one ingest job and publish its outcome — always ack, even on
    failure, same "never silently redeliver an already-attempted job"
    reasoning as app/agent_worker.py::process_request (a redelivered
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
            chunks = ingestor.ingest_text(
                text,
                title=Path(filename).stem,
                ctx=payload["ctx"],
                source=f"upload:{filename}",
                topic=payload.get("topic"),
            )
            await publish_result(client, job_id, {"type": "done", "chunks": chunks})
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            logger.warning(
                "ingest_worker_job_failed",
                extra={"job_id": job_id, "error_class": type(exc).__name__},
            )
            await publish_result(client, job_id, {"type": "error", "content": str(exc)})
        finally:
            await client.xack(INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)


async def run() -> None:
    client = get_client()
    await ensure_consumer_group(client)
    logger.info("ingest_worker_started", extra={"consumer": CONSUMER_NAME})

    # Graceful shutdown — same reasoning as app/agent_worker.py's `run()`:
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
    asyncio.run(run())
