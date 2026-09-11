"""Redis Streams consumer — the worker half of the production ingestion
pipeline. Run one or more of these;
Redis's own consumer-group delivery guarantees each job on
`app/ingestion/ingest_queue.py::INGEST_REQUESTS_STREAM` is handed to exactly one of
them, so running more workers is still one way to add capacity — same shape
as app/turns/agent_worker.py, deliberately a SEPARATE queue/consumer group
from it (see app/ingestion/ingest_queue.py's module docstring for why).

For each job: download the uploaded file from MinIO
(app/ingestion/object_store.py), dispatch to a PDF/DOCX extractor by file extension
(app/ingestion/extractors.py), and feed the resulting text into the SAME
chunk/embed/upsert pipeline every other ingest path already shares
(app/ingestion/ingestor.py::ingest_text) — this worker owns none of that logic
itself, only the download/dispatch/queue-plumbing around it.

Within ONE process, `run()` also runs up to `_MAX_CONCURRENCY` ingest jobs at
once (an `asyncio.Semaphore`-bounded `asyncio.create_task` per job, not a
serial `await` loop) — the exact same shape app/turns/agent_worker.py's `run()`
uses for concurrent turns, see that module's own docstring for the
acquire-before-create-task reasoning shared here verbatim. Safe for the same
reason turns are: a job holds no in-process state a concurrent sibling could
corrupt — each job downloads its own bytes, extracts its own text, and
upserts its own points into Qdrant independently; the module-level MinIO/
Qdrant/embedding clients (`object_store.get_client()`,
app/retrieval/qdrant_store.py, app/retrieval/embeddings.py) are already lazy
singletons shared across sequential jobs today, and remain safe under
concurrent calls from multiple threads (see below).

Download and extraction are both synchronous, blocking calls (the MinIO SDK
and pypdf/python-docx are sync libraries) — unlike a single-job-at-a-time
worker, where calling them directly on the event loop thread was harmless
(nothing else was ever waiting to run), running several jobs concurrently
means a blocking call left on the loop thread would stall every OTHER
in-flight job's I/O (its own Redis reads, its own progress publishes) for as
long as it runs. Both are now offloaded via `asyncio.to_thread`, same as
`ingestor.ingest_text` already was (below) — this is what actually makes
`_MAX_CONCURRENCY > 1` safe, not just the lack of shared state.

The bottleneck this doesn't remove: PDF/DOCX extraction is CPU-bound, and
Python's GIL serializes CPU-bound bytecode across threads regardless of how
many run "concurrently" — `asyncio.to_thread` here buys overlap on the I/O
portions of several jobs (MinIO download, the embedding endpoint's HTTP
round trips, Qdrant upserts, progress publishing) while one job's extraction
runs, not genuine parallel CPU work for the extraction step itself. For a
workload dominated by parsing very large documents, running more WORKER
PROCESSES (or replicas — see Makefile's `ingest-worker` target) still adds
more real CPU parallelism than raising `_MAX_CONCURRENCY` within one process
does; `_MAX_CONCURRENCY` is the right lever when jobs spend more wall-clock
time waiting on MinIO/the embedding endpoint/Qdrant than parsing.

`ingestor.ingest_text` runs via `asyncio.to_thread` for a second reason
beyond the above: so the event loop stays free to actually publish
`on_progress`'s "progress" events (`publish_result`, async, Redis I/O) WHILE
that synchronous embedding loop is still running in the worker thread —
calling it directly on the loop would mean every progress event queues up
behind the whole blocking call and all arrive at once right before "done",
defeating the entire point of a progress bar in `POST /ingest/upload`'s SSE
stream (app/api/main.py). `asyncio.run_coroutine_threadsafe` is the standard
bridge from that worker thread's sync `on_progress` callback back to
publishing on this process's actual event loop.

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

from app.core.config import INGEST_WORKER_MAX_CONCURRENCY
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
_MAX_CONCURRENCY = INGEST_WORKER_MAX_CONCURRENCY  # concurrent ingest jobs ONE
# worker process will run at once (asyncio.Semaphore-bounded, see this
# module's own docstring) — bounds both the semaphore in run() below and how
# many entries a single xreadgroup call pulls off the stream, same shape and
# reasoning as app/turns/agent_worker.py::_READ_COUNT.
_READ_COUNT = _MAX_CONCURRENCY
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
            # Both offloaded via asyncio.to_thread — with several jobs able
            # to run concurrently in this process (see module docstring),
            # calling either directly on the event loop would stall every
            # OTHER in-flight job's I/O for as long as this one's blocking
            # call runs.
            data = await asyncio.to_thread(object_store.download_bytes, payload["object_key"])
            text = await asyncio.to_thread(extractor, data)
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


async def _process_with_limit(
    client, entry_id: str, fields: dict, semaphore: asyncio.Semaphore
) -> None:
    """Runs one job under `semaphore` and releases it when done, success or
    failure — same shape as app/turns/agent_worker.py::_process_with_limit;
    `process_job` already acks in its own `finally` regardless of outcome, so
    the only thing this wrapper owns is the concurrency slot."""
    try:
        await process_job(client, entry_id, fields)
    finally:
        semaphore.release()


async def run() -> None:
    loop = asyncio.get_running_loop()
    # asyncio.to_thread borrows from the loop's DEFAULT executor
    # (min(32, os.cpu_count()+4) if never set) — a generic heuristic with no
    # relationship to _MAX_CONCURRENCY, and hard-capped at 32 regardless of
    # host, which would silently throttle real concurrency below whatever
    # INGEST_WORKER_MAX_CONCURRENCY is configured to if that's ever raised
    # past 32. Sized explicitly instead, to exactly _MAX_CONCURRENCY: a job
    # holds at most ONE pool thread at a time (process_job's to_thread calls
    # are sequential, never overlapping each other), so _MAX_CONCURRENCY
    # concurrent jobs need at most _MAX_CONCURRENCY pool threads — no more,
    # no less. Deliberately NOT also padded by CPU core count: the GIL, not
    # thread count, is what bounds CPU-bound throughput regardless of cores
    # (see this module's own docstring), and _MAX_CONCURRENCY is already
    # the operator's own CPU-vs-I/O-aware tuning knob (WORKER_CONCURRENCY.md)
    # — a core-count-based cap here would just silently override that
    # deliberate setting the same way the default executor's own ceiling
    # does, which is exactly what this is replacing.
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

    # Graceful shutdown — same reasoning as app/turns/agent_worker.py's `run()`:
    # a SIGTERM/SIGINT stops this worker from claiming a NEW job, but never
    # interrupts one already in flight (a redelivered ingest job would
    # re-embed and re-upsert the same document's chunks a second time, a
    # real data-quality regression — see process_job's own docstring).
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Bounds how many jobs this ONE process runs at once. Acquired in the
    # read loop below, BEFORE a task is even created — not inside the task —
    # so a full semaphore also backpressures reading: this process simply
    # stops pulling new entries off the stream once it's at capacity, leaving
    # them pending for the group (another worker, or this one once a slot
    # frees up, can still claim them) rather than piling up an unbounded
    # number of not-yet-running tasks in `in_flight` below. Same shape as
    # app/turns/agent_worker.py::run().
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
    await client.aclose()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-ingest-worker")
    asyncio.run(run())
