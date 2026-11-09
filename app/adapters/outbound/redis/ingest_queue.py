"""The `IngestQueue` adapter (`app/ports/outbound/jobs.py`): Redis Streams for the ingestion pipeline, a SEPARATE stream and consumer group from the chat-turn queue
(`queue.py`). Deliberate: chat turns are short and latency-sensitive, an ingest job (parsing a large PDF) can run for many seconds, and a shared consumer group
round-robins without priority, so a burst of ingest jobs would delay chat turns. Same Redis server and client as `queue.py` (`app/platform/redis_client.py`); only the
stream and group names and the payloads are separate.

This module is the PRODUCER half: enqueue a job and read its events back, for `app/adapters/inbound/api/routers/ingest.py` (`POST /ingest/upload`). The consumer is
`app/adapters/inbound/workers/ingest_worker.py` (with `ingest_streams.py`), the only place that downloads from the object store and runs extraction.
"""
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any, cast

import redis.asyncio as redis

from app.core import metrics
from app.core.security import SecurityCtx
from app.platform.redis_client import StreamReadResponse, get_client
from app.ports.outbound.jobs import INGEST_REQUESTS_STREAM, ingest_results_stream_key

logger = logging.getLogger(__name__)


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
    already-uploaded file in MinIO (app/adapters/outbound/minio/object_store.py) — the job
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


async def read_results(
    client: redis.Redis,
    job_id: str,
    *,
    block_ms: int = 5000,
    first_event_deadline_seconds: float | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Producer side: yield each event published for `job_id`, in order,
    until a terminal event (`type` is `done` or `error`) is seen, then
    return — same shape as app/adapters/outbound/redis/queue.py::read_results,
    including its own `first_event_deadline_seconds` (see that function's
    docstring): bounds only the wait for the FIRST event ever published
    (i.e. "was this job picked up by any worker at all"), never the
    job's own real duration — an ingest job's `started` event arrives
    almost immediately after pickup, well before extraction/embedding,
    which can legitimately run for minutes on a large file."""
    key = ingest_results_stream_key(job_id)
    last_id = "0"
    deadline = (
        time.monotonic() + first_event_deadline_seconds
        if first_event_deadline_seconds is not None
        else None
    )
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            # Same signal as app/adapters/outbound/redis/queue.py::read_results — see there.
            metrics.agent_worker_unreachable_total.labels(queue="ingest").inc()
            yield {
                "type": "error",
                "content": (
                    f"No response for job {job_id!r} after "
                    f"{first_event_deadline_seconds:.0f}s — is an ingest-worker running?"
                ),
            }
            return
        response = cast(StreamReadResponse, await client.xread({key: last_id}, block=block_ms, count=10))
        if not response:
            continue
        deadline = None  # a real event arrived — no longer "nobody is listening"
        _, entries = response[0]
        for entry_id, fields in entries:
            last_id = entry_id
            event = json.loads(fields["payload"])
            yield event
            if event.get("type") in ("done", "error"):
                return


async def delete_results_stream(client: redis.Redis, job_id: str) -> None:
    """Best-effort cleanup — see app/adapters/outbound/redis/queue.py::delete_results_stream for
    why this isn't required for correctness, only for not waiting out the
    full TTL on the common path."""
    try:
        await client.delete(ingest_results_stream_key(job_id))
    except Exception as exc:  # noqa: BLE001 - cleanup is optional, never worth failing a job over
        logger.warning("ingest_queue_results_cleanup_failed", extra={"error_class": type(exc).__name__})


class RedisIngestQueue:
    """The `IngestQueue` adapter. The module's functions are looked up at call time, which is how their tests patch `get_client`."""

    async def publish_ingest_request(
        self, *, job_id: str, object_key: str, filename: str, content_type: str, ctx: SecurityCtx, topic: str | None = None
    ) -> None:
        await publish_ingest_request(
            get_client(), job_id=job_id, object_key=object_key, filename=filename, content_type=content_type, ctx=ctx, topic=topic
        )

    async def read_results(self, job_id: str, *, first_event_deadline_seconds: float | None = None) -> AsyncGenerator[dict[str, Any], None]:
        async for event in read_results(get_client(), job_id, first_event_deadline_seconds=first_event_deadline_seconds):
            yield event

    async def delete_results_stream(self, job_id: str) -> None:
        await delete_results_stream(get_client(), job_id)
