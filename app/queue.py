"""Redis Streams as a persistent message queue between the SSE-serving
process and one or more agent-executing worker processes (GRAPH_PATTERNS.md
pattern 43) — so the two can scale independently: run more `uvicorn`
processes to handle more concurrent SSE connections, run more
`app/agent_worker.py` processes to handle more concurrent turns, without
either number depending on the other.

Two streams per turn:
- `REQUESTS_STREAM` (`agent:requests`) — ONE shared stream, consumed via a
  Redis Streams consumer group (`CONSUMER_GROUP`) so N worker processes
  split the load with no request delivered to more than one worker (Redis's
  own consumer-group semantics, not something this module has to implement).
- A per-request results stream (`results_stream_key(request_id)`) — created
  fresh for each turn, written to by whichever worker picks up the request,
  read by the producer that published it, and `EXPIRE`d so an abandoned one
  (a client that disconnects mid-stream) self-cleans rather than leaking
  forever — the same TTL-on-write shape app/semantic_cache.py already uses
  for its own Redis keys, applied here for cleanup instead of caching.

This module only wraps the queue mechanics (publish/read, group setup) —
it doesn't know what a "turn" or an "event" IS. The producer half lives in
app/api.py (`POST /chat/stream/queued`); the consumer half is
app/agent_worker.py, which is the only place that actually runs the graph.
"""
import json
import logging

import redis.asyncio as redis

from app.config import REDIS_URL

logger = logging.getLogger(__name__)

REQUESTS_STREAM = "agent:requests"
CONSUMER_GROUP = "agent-workers"
RESULTS_STREAM_TTL_SECONDS = 300  # an abandoned results stream self-expires after 5 minutes

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """A separate client/connection from app/semantic_cache.py's (module-
    level singleton there too, but that one deliberately does NOT decode
    responses, since it stores raw vector bytes) — this module's payloads
    are always JSON text, so decode_responses=True here keeps every caller
    working with plain str, never bytes.

    `socket_timeout=None` is deliberate, not an oversight: redis-py
    defaults `socket_timeout` to 5 SECONDS — verified empirically that this
    races directly against `XREAD`/`XREADGROUP`'s own server-side `BLOCK`
    (both `read_results` and app/agent_worker.py block for 5000ms), so the
    socket timed out and raised `redis.exceptions.TimeoutError` before
    Redis's own block window ever elapsed, on every real blocking read.
    Redis's `BLOCK` argument is what actually bounds the wait; the socket
    itself is left to wait as long as that takes. `socket_connect_timeout`
    stays at its normal finite default so an unreachable Redis still fails
    fast on connect, just not on a legitimate long read.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=None)
    return _client


def results_stream_key(request_id: str) -> str:
    return f"agent:results:{request_id}"


async def ensure_consumer_group(client: redis.Redis) -> None:
    """Idempotent: creates the shared consumer group (and its stream, via
    mkstream) on first use, in whichever worker process reaches it first —
    same idempotent-setup shape app/semantic_cache.py's `_ensure_index`
    already uses for its RediSearch index. `id="0"` means a newly-started
    consumer group sees every request already on the stream, not just ones
    published after it was created — the right default for a demo where a
    worker starting after the API process shouldn't silently skip a
    request that arrived first."""
    try:
        await client.xgroup_create(REQUESTS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish_request(
    client: redis.Redis,
    *,
    request_id: str,
    text: str,
    thread_id: str,
    ctx: dict,
    require_approval: bool = False,
    images: list[str] | None = None,
) -> None:
    """Producer side: enqueue one turn request. `request_id` is generated
    by the CALLER (app/api.py), not here — the producer needs to know it
    BEFORE publishing, so it can start listening on
    `results_stream_key(request_id)` with no race against a worker that
    might otherwise finish (and write results) before the producer even
    starts reading. `images` (GRAPH_PATTERNS.md pattern 44) rides along
    as plain JSON strings (URLs/data URIs) — Redis Streams payloads are
    already JSON text, so this needs no different handling than any
    other field here."""
    payload = json.dumps(
        {
            "request_id": request_id,
            "text": text,
            "thread_id": thread_id,
            "ctx": ctx,
            "require_approval": require_approval,
            "images": images or [],
        }
    )
    await client.xadd(REQUESTS_STREAM, {"payload": payload})


async def publish_result(client: redis.Redis, request_id: str, event: dict) -> None:
    """Consumer side: append one typed event (the same shapes
    app/agent.py::_run_graph_stream already yields — token, tool_start,
    tool_end, citations, error, done) to this request's results stream,
    refreshing its TTL on every write so a long-running turn's stream
    doesn't expire mid-flight."""
    key = results_stream_key(request_id)
    await client.xadd(key, {"payload": json.dumps(event)})
    await client.expire(key, RESULTS_STREAM_TTL_SECONDS)


async def read_results(client: redis.Redis, request_id: str, *, block_ms: int = 5000):
    """Producer side: yield each event published for `request_id`, in
    order, from the start of its (fresh, per-request) stream — blocking up
    to `block_ms` per read while waiting for the next one — until a
    terminal event (`type` is `done` or `error`) is seen, then return.
    A caller that stops iterating early (e.g. the client disconnected) just
    leaves the stream to expire via its TTL rather than needing explicit
    cleanup here.
    """
    key = results_stream_key(request_id)
    last_id = "0"
    while True:
        response = await client.xread({key: last_id}, block=block_ms, count=10)
        if not response:
            continue  # no new entries within block_ms — poll again
        _, entries = response[0]
        for entry_id, fields in entries:
            last_id = entry_id
            event = json.loads(fields["payload"])
            yield event
            if event.get("type") in ("done", "error"):
                return


async def delete_results_stream(client: redis.Redis, request_id: str) -> None:
    """Best-effort cleanup once the producer has consumed a genuine
    terminal event — not required for correctness (the TTL above already
    bounds a leaked stream's lifetime), just avoids waiting out the full
    TTL on the common path where everything finished normally."""
    try:
        await client.delete(results_stream_key(request_id))
    except Exception as exc:  # noqa: BLE001 - cleanup is optional, never worth failing a turn over
        logger.warning("queue_results_cleanup_failed", extra={"error_class": type(exc).__name__})
