"""The ingest worker's side of its Redis Streams queue: creating the consumer group and publishing a job's events.

Separate from the chat queue's (`streams.py`) on purpose, as the streams themselves are (`app/ports/outbound/jobs.py`). The worker owns this; the producer
side is `app/adapters/outbound/redis/ingest_queue.py`.
"""
import json

import redis.asyncio as redis

from app.ports.outbound.jobs import (
    INGEST_CONSUMER_GROUP,
    INGEST_REQUESTS_STREAM,
    RESULTS_STREAM_TTL_SECONDS,
    ingest_results_stream_key,
)


async def ensure_consumer_group(client: redis.Redis) -> None:
    """Idempotent — same shape as app/adapters/outbound/redis/queue.py::ensure_consumer_group,
    against this module's own stream/group instead."""
    try:
        await client.xgroup_create(INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish_result(client: redis.Redis, job_id: str, event: dict) -> None:
    """Consumer side: append one typed event (see
    app/adapters/inbound/workers/ingest_worker.py::process_job for the shapes — started, done,
    error) to this job's results stream, refreshing its TTL on every
    write so a slow extraction doesn't let the stream expire mid-job."""
    key = ingest_results_stream_key(job_id)
    await client.xadd(key, {"payload": json.dumps(event)})
    await client.expire(key, RESULTS_STREAM_TTL_SECONDS)
