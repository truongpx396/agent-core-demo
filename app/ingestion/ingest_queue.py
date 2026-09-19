"""Redis Streams queue for the production ingestion pipeline — a SEPARATE
stream/consumer group from `app/job_queue/queue.py`'s chat-turn queue
(`agent:requests`). Deliberate: chat turns are short/latency-sensitive,
while an ingest job (parsing a large PDF) can run for many seconds, and a
shared consumer group round-robins without job-type priority, so a burst
of ingest jobs would delay chat turns — same "independently scalable"
split `app/job_queue/queue.py` already applies between the SSE and
agent-executing tiers.

Reuses `app/job_queue/queue.py`'s `get_client()` (same Redis server/settings)
rather than a second connection pool — only the stream/group names and
payload shapes here are actually separate.

Producer: `app/api/main.py` (`POST /ingest/upload`). Consumer:
`app/ingestion/ingest_worker.py`, the only place that downloads from MinIO
and runs extraction/ingestion.
"""
import json
import logging
from typing import cast

import redis.asyncio as redis

from app.core.security import SecurityCtx
from app.job_queue.queue import (
    StreamReadResponse,
    get_client,  # noqa: F401 - re-exported for callers that only need one import
)

logger = logging.getLogger(__name__)

INGEST_REQUESTS_STREAM = "ingest:requests"
INGEST_CONSUMER_GROUP = "ingest-workers"
RESULTS_STREAM_TTL_SECONDS = 300  # same TTL reasoning as app/job_queue/queue.py's chat-results streams


def results_stream_key(job_id: str) -> str:
    return f"ingest:results:{job_id}"


async def ensure_consumer_group(client: redis.Redis) -> None:
    """Idempotent — same shape as app/job_queue/queue.py::ensure_consumer_group,
    against this module's own stream/group instead."""
    try:
        await client.xgroup_create(INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish_ingest_request(
    client: redis.Redis,
    *,
    job_id: str,
    object_key: str,
    filename: str,
    content_type: str,
    ctx: SecurityCtx,
    topic: str | None = None,
) -> None:
    """Producer side: enqueue one ingest job. `object_key` points at the
    already-uploaded file in MinIO (app/ingestion/object_store.py) — the job
    payload carries a pointer, never the file bytes themselves, keeping
    Redis Streams entries small regardless of document size."""
    payload = json.dumps(
        {
            "job_id": job_id,
            "object_key": object_key,
            "filename": filename,
            "content_type": content_type,
            "ctx": ctx,
            "topic": topic,
        }
    )
    await client.xadd(INGEST_REQUESTS_STREAM, {"payload": payload})


async def publish_result(client: redis.Redis, job_id: str, event: dict) -> None:
    """Consumer side: append one typed event (see
    app/ingestion/ingest_worker.py::process_job for the shapes — started, done,
    error) to this job's results stream, refreshing its TTL on every
    write so a slow extraction doesn't let the stream expire mid-job."""
    key = results_stream_key(job_id)
    await client.xadd(key, {"payload": json.dumps(event)})
    await client.expire(key, RESULTS_STREAM_TTL_SECONDS)


async def read_results(client: redis.Redis, job_id: str, *, block_ms: int = 5000):
    """Producer side: yield each event published for `job_id`, in order,
    until a terminal event (`type` is `done` or `error`) is seen, then
    return — same shape as app/job_queue/queue.py::read_results."""
    key = results_stream_key(job_id)
    last_id = "0"
    while True:
        response = cast(StreamReadResponse, await client.xread({key: last_id}, block=block_ms, count=10))
        if not response:
            continue
        _, entries = response[0]
        for entry_id, fields in entries:
            last_id = entry_id
            event = json.loads(fields["payload"])
            yield event
            if event.get("type") in ("done", "error"):
                return


async def delete_results_stream(client: redis.Redis, job_id: str) -> None:
    """Best-effort cleanup — see app/job_queue/queue.py::delete_results_stream for
    why this isn't required for correctness, only for not waiting out the
    full TTL on the common path."""
    try:
        await client.delete(results_stream_key(job_id))
    except Exception as exc:  # noqa: BLE001 - cleanup is optional, never worth failing a job over
        logger.warning("ingest_queue_results_cleanup_failed", extra={"error_class": type(exc).__name__})
