"""Redis Streams consumer — the worker half of the production ingestion
pipeline. Run one or more of these; Redis's consumer-group delivery hands
each job on `ingest_queue.py::INGEST_REQUESTS_STREAM` to exactly one worker,
so running more is how you add capacity — same shape as
`app/job_queue/agent_worker.py`, deliberately a separate queue/group (see
`ingest_queue.py`'s docstring for why).

Per job: download from MinIO (`object_store.py`), dispatch to a PDF/DOCX
extractor by extension (`extractors.py`), feed the text into the same
chunk/embed/upsert pipeline every ingest path shares
(`ingestor.py::ingest_text`) — this worker only owns the download/dispatch/
queue plumbing.

A worker that dies mid-job leaves its entry pending for the consumer group
forever unless something reclaims it — `_reclaim_loop` (run alongside the
main read loop by every worker in the pool) does that via `XAUTOCLAIM`,
same mechanism and "never blindly redeliver" policy as
`app/job_queue/agent_worker.py`'s own reclaim loop; see
`_handle_reclaimed_job`'s docstring for why.

`run()` runs up to `_MAX_CONCURRENCY` jobs at once per process (an
`asyncio.Semaphore`-bounded `create_task` per job, acquired before the task
is created — same shape and reasoning as `agent_worker.py::run()`). Safe
because a job holds no shared in-process state; module-level MinIO/Qdrant/
embedding clients are already lazy singletons safe under concurrent use.

Download and extraction are sync/blocking (MinIO SDK, pypdf/python-docx),
so both are offloaded via `asyncio.to_thread` — otherwise one job's blocking
call would stall every other in-flight job's I/O on the event loop. This
doesn't remove the GIL bottleneck though: PDF/DOCX extraction is CPU-bound,
and the GIL serializes bytecode across threads regardless of concurrency —
`to_thread` overlaps I/O (MinIO download, embedding HTTP calls, Qdrant
upserts) while one job parses, not real parallel CPU work. For workloads
dominated by parsing large documents, more worker PROCESSES add more real
CPU parallelism than raising `_MAX_CONCURRENCY`; raise `_MAX_CONCURRENCY`
when jobs spend more time waiting on MinIO/embeddings/Qdrant than parsing.

`ingestor.ingest_text` is `async def` (real `AsyncQdrantClient`/`AsyncOpenAI`
under the hood) so it awaits its own I/O directly on this loop rather than
needing a thread. Its `on_progress` callback (`_make_progress_reporter`
below) is likewise a plain `async def` that calls `publish_result` in
place — no `asyncio.run_coroutine_threadsafe` bridge needed.

Run with: `python -m app.ingestion.ingest_worker` (Makefile's `ingest-worker`
target). Needs `make up`'s Redis + MinIO; not started by `make up` itself —
opt-in alongside `POST /ingest/upload` (app/api/main.py), which only
publishes the job.
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

from app.core import metrics
from app.core.config import (
    INGEST_WORKER_MAX_CONCURRENCY,
    INGEST_WORKER_RECLAIM_IDLE_SECONDS,
    WORKER_RECLAIM_INTERVAL_SECONDS,
)
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
from app.job_queue.queue import (
    StreamReadResponse,
    publish_dead_letter,
    reclaim_stale_entries,
)

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_MAX_CONCURRENCY = INGEST_WORKER_MAX_CONCURRENCY  # concurrent jobs per worker
# process (see module docstring) — also bounds how many entries one
# xreadgroup call pulls, same shape as agent_worker.py::_READ_COUNT.
_READ_COUNT = _MAX_CONCURRENCY
_BLOCK_MS = 5000


def _make_progress_reporter(client, job_id: str):
    """An `async def` callback `ingestor.ingest_text` awaits directly (it
    runs on this worker's own event loop, so no thread-bridging needed).
    Fire-and-forget in spirit, not mechanism: a failed progress publish is
    logged, never raised — the terminal `done`/`error` event published
    below is what actually matters for correctness."""

    async def on_progress(done: int, total: int) -> None:
        try:
            await publish_result(client, job_id, {"type": "progress", "done": done, "total": total})
        except Exception as exc:  # noqa: BLE001 - a dropped progress tick must not fail the ingest
            logger.warning(
                "ingest_worker_progress_publish_failed",
                extra={"job_id": job_id, "error_class": type(exc).__name__},
            )

    return on_progress


async def process_job(client, entry_id: str, fields: dict) -> None:
    """Run one ingest job and publish its outcome — always ack, even on
    failure, same "never redeliver an already-attempted job" reasoning as
    `agent_worker.py::process_request`: a redelivered job would re-embed
    and re-upsert the same document's chunks, duplicating them in the
    index."""
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
            # Both offloaded via asyncio.to_thread — see module docstring:
            # a blocking call on the event loop would stall every OTHER
            # in-flight job's I/O.
            data = await asyncio.to_thread(object_store.download_bytes, payload["object_key"])
            text = await asyncio.to_thread(extractor, data)
            on_progress = _make_progress_reporter(client, job_id)
            chunks = await ingestor.ingest_text(
                text,
                title=Path(filename).stem,
                ctx=payload["ctx"],
                source=f"upload:{filename}",
                topic=payload.get("topic"),
                on_progress=on_progress,
            )
            await publish_result(client, job_id, {"type": "done", "chunks": chunks})
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            # Log the actual message, not just error_class (same truncated-
            # string convention as sandbox_session.py/skills.py) — otherwise
            # the failure reason only lives in the results stream, which
            # expires after RESULTS_STREAM_TTL_SECONDS.
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


async def _process_with_limit(
    client, entry_id: str, fields: dict, semaphore: asyncio.Semaphore
) -> None:
    """Runs one job under `semaphore` and releases it when done, success or
    failure — same shape as `agent_worker.py::_process_with_limit`;
    `process_job` already acks regardless of outcome, so this wrapper only
    owns the concurrency slot."""
    try:
        await process_job(client, entry_id, fields)
    finally:
        semaphore.release()


async def _handle_reclaimed_job(client, entry_id: str, fields: dict) -> None:
    """One entry `_reclaim_loop` found idle past
    `INGEST_WORKER_RECLAIM_IDLE_SECONDS` — its original worker almost
    certainly died mid-job (crashed, OOM-killed) before ever reaching
    `process_job`'s own ack. NOT re-run: it may already have upserted some
    chunks into the vector index, and redelivering it would duplicate them
    — the same reason `process_job` always acks instead of ever letting a
    failure be retried. Same policy as `agent_worker.py`'s own
    `_handle_reclaimed_job`: surface an error, archive to a dead-letter
    stream, ack."""
    try:
        payload = json.loads(fields["payload"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "ingest_worker_reclaimed_payload_unreadable",
            extra={"entry_id": entry_id, "error_class": type(exc).__name__},
        )
        payload = {}
    else:
        job_id = payload.get("job_id")
        logger.warning(
            "ingest_worker_job_reclaimed",
            # NOT "filename" — that key collides with LogRecord's own
            # built-in attribute of the same name and raises KeyError from
            # inside logging's makeRecord.
            extra={"entry_id": entry_id, "job_id": job_id, "upload_filename": payload.get("filename")},
        )
        if job_id:
            await publish_result(
                client,
                job_id,
                {
                    "type": "error",
                    "content": (
                        "The worker processing this upload stopped responding "
                        "before it finished. Please upload it again."
                    ),
                },
            )
    metrics.agent_worker_job_reclaimed_total.labels(queue="ingest").inc()
    await publish_dead_letter(
        client,
        requests_stream=INGEST_REQUESTS_STREAM,
        entry_id=entry_id,
        payload=payload,
        reason="worker_lost",
    )
    await client.xack(INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)


async def _reclaim_loop(client, stop_event: asyncio.Event) -> None:
    """Same shape and reasoning as `agent_worker.py::_reclaim_loop` — see
    there for why this doesn't just redeliver a reclaimed job, and why a
    transient Redis error here is logged and retried next interval rather
    than ending the loop."""
    min_idle_ms = INGEST_WORKER_RECLAIM_IDLE_SECONDS * 1000
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WORKER_RECLAIM_INTERVAL_SECONDS)
            return  # stop_event was set during the wait — shutting down
        except TimeoutError:
            pass  # normal case: interval elapsed, run a pass below
        try:
            entries = await reclaim_stale_entries(
                client,
                stream=INGEST_REQUESTS_STREAM,
                group=INGEST_CONSUMER_GROUP,
                consumer=CONSUMER_NAME,
                min_idle_ms=min_idle_ms,
            )
            for entry_id, fields in entries:
                await _handle_reclaimed_job(client, entry_id, fields)
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end reclaim for this process's whole life
            logger.warning("ingest_worker_reclaim_pass_failed", extra={"error_class": type(exc).__name__})


async def run() -> None:
    loop = asyncio.get_running_loop()
    # asyncio.to_thread borrows the loop's DEFAULT executor
    # (min(32, os.cpu_count()+4)) — capped at 32 regardless of host, which
    # would silently throttle concurrency if INGEST_WORKER_MAX_CONCURRENCY
    # is raised past that. Sized explicitly to _MAX_CONCURRENCY instead: a
    # job holds at most one pool thread at a time, so N concurrent jobs need
    # at most N threads. Not padded by CPU count — the GIL, not thread
    # count, bounds CPU-bound throughput (see module docstring), and
    # _MAX_CONCURRENCY is already the operator's tuning knob
    # (WORKER_CONCURRENCY.md).
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_CONCURRENCY, thread_name_prefix="ingest-worker"
        )
    )

    client = get_client()
    await ensure_consumer_group(client)
    logger.info(
        "ingest_worker_started",
        extra={"consumer": CONSUMER_NAME, "max_concurrency": _MAX_CONCURRENCY},
    )

    # Graceful shutdown — same reasoning as agent_worker.py's run(): SIGTERM/
    # SIGINT stops this worker from claiming a NEW job but never interrupts
    # one in flight (a redelivered job would re-embed/re-upsert the same
    # chunks — see process_job's docstring). A worker that dies WITHOUT this
    # graceful path still abandons its claimed entry — _reclaim_loop below,
    # run by every replica in the pool, recovers from that case.
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    reclaim_task = asyncio.create_task(_reclaim_loop(client, stop_event))

    # Bounds jobs this process runs at once. Acquired BEFORE the task is
    # created, so a full semaphore also backpressures reading: entries stay
    # pending for the group instead of piling up unbounded in `in_flight`.
    # Same shape as agent_worker.py::run().
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    in_flight: set[asyncio.Task] = set()

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
            await semaphore.acquire()
            task = asyncio.create_task(_process_with_limit(client, entry_id, fields, semaphore))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

    logger.info(
        "ingest_worker_stopping", extra={"consumer": CONSUMER_NAME, "in_flight": len(in_flight)}
    )
    # Let every already-claimed job finish (and ack) instead of abandoning it
    # mid-job — see the graceful-shutdown comment above.
    if in_flight:
        await asyncio.gather(*in_flight)
    await reclaim_task  # stop_event already set above, so this returns promptly
    await client.aclose()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-ingest-worker")
    asyncio.run(run())
