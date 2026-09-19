"""Redis Streams as a persistent message queue between the SSE-serving
process and one or more agent-executing worker processes (GRAPH_PATTERNS.md
pattern 43) — so the two scale independently: more `uvicorn` processes for
concurrent SSE connections, more `app/job_queue/agent_worker.py` processes
for concurrent turns.

Two streams per JOB — a job is one of three kinds (`"turn"`/`"resume"`/
`"cancel"`), each with its own fresh request_id/results pair, never shared
across jobs even on the SAME thread_id:
- `requests_stream_key(domain)` (`agent:requests:{domain}`) — one shared
  stream PER DOMAIN, consumed via a Redis consumer group (`CONSUMER_GROUP`)
  so N workers bound to that domain (`AGENT_DOMAIN`) split its load, with
  no request delivered twice (Redis's own semantics). Splitting by domain
  is what lets `app/api/main.py` stay one unified process routing by
  `X-Domain` while each domain's worker pool scales independently —
  pattern 43's SSE/worker split, one level deeper.
- A per-request results stream (`results_stream_key(request_id)`) —
  created fresh per job, written by whichever worker picks it up, read by
  the producer, `EXPIRE`d so an abandoned one (client disconnects
  mid-stream) self-cleans — same TTL-on-write shape as
  `app/retrieval/semantic_cache.py`'s keys.

A separate, per-THREAD mechanism: the cancel flag (`cancel_flag_key`/
`set_cancel_flag`/`is_cancelled`/`clear_cancel_flag`) — a short-lived key a
`"turn"` job's worker polls between graph events, so `POST /chat/cancel`
can stop an ACTIVELY STREAMING turn (which has no queued job to target,
unlike one paused at human_approval — see `agent_worker.py`'s `"cancel"`
dispatch for that case).

This module only wraps queue mechanics (publish/read, group setup) — it
doesn't know what a "turn" or "event" IS. Producer: `app/api/main.py`
(`POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel`). Consumer:
`app/job_queue/agent_worker.py`, the only place that runs the graph.
"""
import json
import logging
from typing import cast

import redis.asyncio as redis

from app.core.config import REDIS_MAX_CONNECTIONS, REDIS_URL
from app.core.security import SecurityCtx

logger = logging.getLogger(__name__)

# The actual shape of a redis-py Streams read response with
# decode_responses=True — [(stream_name, [(entry_id, {field: value}), ...])].
# redis-py's stubs type xread/xreadgroup far wider (can't key an overload on
# a runtime instance attribute); every client here is always constructed
# with decode_responses=True (see get_client below), so this cast just
# narrows what's unprovable to the type checker. Shared with
# app/ingestion/ingest_queue.py and app/ingestion/ingest_worker.py, which
# read streams the same way.
StreamReadResponse = list[tuple[str, list[tuple[str, dict[str, str]]]]]

CONSUMER_GROUP = "agent-workers"
RESULTS_STREAM_TTL_SECONDS = 300  # an abandoned results stream self-expires after 5 minutes
CANCEL_FLAG_TTL_SECONDS = 60  # outlasts any gap between a worker's cancel-check
# polls; short enough that a flag nobody consumed (thread finished before
# /chat/cancel ran) doesn't linger and spuriously cancel a later turn.

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """A separate client from `semantic_cache.py`'s (also a module-level
    singleton, but that one deliberately does NOT decode responses since it
    stores raw vector bytes) — this module's payloads are always JSON text,
    so `decode_responses=True` keeps every caller working with plain str.

    `socket_timeout=None` is deliberate: redis-py's 5s default races
    against `XREAD`/`XREADGROUP`'s own server-side `BLOCK` (both
    `read_results` and `agent_worker.py` block for 5000ms), raising
    `TimeoutError` before Redis's own block window elapses. `BLOCK` is what
    actually bounds the wait. `socket_connect_timeout` stays at its normal
    default so an unreachable Redis still fails fast on connect.

    `max_connections=REDIS_MAX_CONNECTIONS`: redis-py's default (100) is
    easy to blow through here — every `POST /chat/stream/queued` SSE
    connection holds a pooled connection for the FULL blocking-read
    duration of its turn, not a quick round trip. Verified: 250 concurrent
    requests against the unset default produced `MaxConnectionsError` past
    the ~100th (an 82% failure rate), regardless of agent_worker.py
    concurrency settings.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=None,
            max_connections=REDIS_MAX_CONNECTIONS,
        )
    return _client


def results_stream_key(request_id: str) -> str:
    return f"agent:results:{request_id}"


def requests_stream_key(domain: str = "ecorp") -> str:
    """The one requests stream a domain's worker pool (`AGENT_DOMAIN`) all
    consume from as a single consumer group — see module docstring for why
    domain-scoping keeps each pool's load independent. Defaults to
    `"ecorp"`, same convention as `runtime.py::init_graph_async` and
    `AGENT_DOMAIN`'s own default."""
    return f"agent:requests:{domain}"


async def ensure_consumer_group(client: redis.Redis, domain: str = "ecorp") -> None:
    """Idempotent: creates domain's consumer group (and stream, via
    mkstream) on first use, same shape as `semantic_cache.py::_ensure_index`.
    `id="0"` means a newly-started group sees every request already on the
    stream, not just later ones — so a worker starting after the API
    process doesn't skip a request that arrived first."""
    try:
        await client.xgroup_create(
            requests_stream_key(domain), CONSUMER_GROUP, id="0", mkstream=True
        )
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish_request(
    client: redis.Redis,
    *,
    request_id: str,
    text: str,
    thread_id: str,
    ctx: SecurityCtx,
    domain: str = "ecorp",
    require_approval: bool = False,
    images: list[str] | None = None,
) -> None:
    """Producer side: enqueue one new-turn (`"kind": "turn"`) request onto
    `domain`'s requests stream — only a worker pool with matching
    `AGENT_DOMAIN` reads it. `request_id` is generated by the CALLER
    (app/api/main.py) so it can start listening on
    `results_stream_key(request_id)` before publishing, with no race
    against a worker finishing first. `images` (pattern 44) rides along as
    plain JSON strings (URLs/data URIs) — no special handling needed.

    `domain` never rides IN the payload — the stream it landed on already
    tells the worker which domain it's for.

    Sibling functions `publish_resume_request`/`publish_cancel_request`
    publish the other two job kinds onto this SAME per-domain stream (one
    consumer group per domain, dispatch-by-`kind` in `agent_worker.py`)
    rather than a queue per kind. A resume/cancel MUST land on the SAME
    domain's stream as the original turn — the paused graph was built from
    that domain's manifest/tools, so a different domain's worker resuming
    it would run the wrong tool universe; `X-Domain` keeps all three calls
    for one conversation aimed at the same domain.
    """
    payload = json.dumps(
        {
            "kind": "turn",
            "request_id": request_id,
            "text": text,
            "thread_id": thread_id,
            "ctx": ctx,
            "require_approval": require_approval,
            "images": images or [],
        }
    )
    await client.xadd(requests_stream_key(domain), {"payload": payload})


async def publish_resume_request(
    client: redis.Redis,
    *,
    request_id: str,
    thread_id: str,
    approved: bool,
    ctx: SecurityCtx,
    domain: str = "ecorp",
) -> None:
    """Producer side: enqueue a resume decision for a turn paused at
    human_approval — the queued-path counterpart to
    `runtime_stream.py::astream_events_resume`. Any worker in `domain`'s
    pool can pick it up: the checkpoint lives in Postgres, not worker
    memory (see `publish_request`'s docstring for why `domain` must match
    the original turn's)."""
    payload = json.dumps(
        {
            "kind": "resume",
            "request_id": request_id,
            "thread_id": thread_id,
            "approved": approved,
            "ctx": ctx,
        }
    )
    await client.xadd(requests_stream_key(domain), {"payload": payload})


async def publish_cancel_request(
    client: redis.Redis, *, request_id: str, thread_id: str, ctx: SecurityCtx, domain: str = "ecorp"
) -> None:
    """Producer side: enqueue a cancel for a turn paused at human_approval
    (queued-path counterpart to `runtime_stream.py::cancel_run`).
    Deliberately not how an ACTIVELY STREAMING turn gets cancelled — that's
    the cancel-flag mechanism below; this is a no-op in that case
    (`cancel_run` returns False). `POST /chat/cancel` always does both: set
    the flag AND publish this, so whichever mechanism applies handles it."""
    payload = json.dumps(
        {"kind": "cancel", "request_id": request_id, "thread_id": thread_id, "ctx": ctx}
    )
    await client.xadd(requests_stream_key(domain), {"payload": payload})


def cancel_flag_key(thread_id: str) -> str:
    return f"agent:cancel:{thread_id}"


async def set_cancel_flag(client: redis.Redis, thread_id: str) -> None:
    """Signal an ACTIVELY STREAMING `"turn"` job's worker to stop at its
    next cancel-check (`runtime_stream.py::_iterate_with_timeout`'s
    `cancel_check`) — a short-lived flag, not a queued job, since that
    worker is polling this key directly, not waiting on the queue."""
    await client.set(cancel_flag_key(thread_id), "1", ex=CANCEL_FLAG_TTL_SECONDS)


async def is_cancelled(client: redis.Redis, thread_id: str) -> bool:
    return bool(await client.get(cancel_flag_key(thread_id)))


async def clear_cancel_flag(client: redis.Redis, thread_id: str) -> None:
    """Called right before a worker starts a NEW `"turn"` job for a
    thread_id — clears any stale flag left from a PRIOR turn (e.g.
    /chat/cancel raced with that turn finishing on its own) so it can't
    spuriously cancel this new one."""
    await client.delete(cancel_flag_key(thread_id))


async def publish_result(client: redis.Redis, request_id: str, event: dict) -> None:
    """Consumer side: append one typed event (same shapes
    `runtime_stream.py::_run_graph_stream` yields — token, tool_start,
    tool_end, citations, error, done) to this request's results stream,
    refreshing its TTL so a long-running turn doesn't expire mid-flight."""
    key = results_stream_key(request_id)
    await client.xadd(key, {"payload": json.dumps(event)})
    await client.expire(key, RESULTS_STREAM_TTL_SECONDS)


async def read_results(client: redis.Redis, request_id: str, *, block_ms: int = 5000):
    """Producer side: yield each event published for `request_id`, in
    order, blocking up to `block_ms` per read, until a terminal event
    (`type` is `done`, `error`, or `approval_required`) is seen. Treating
    `approval_required` as terminal matters: it's the last event
    `_run_graph_stream` yields for a paused turn, and the worker's own
    forwarding loop stops right after publishing it — resuming is a
    SEPARATE job (`publish_resume_request`) with its own results stream, so
    without this the generator would block forever waiting for a done/error
    a paused worker will never send. A caller that stops iterating early
    just leaves the stream to expire via TTL.
    """
    key = results_stream_key(request_id)
    last_id = "0"
    while True:
        response = cast(StreamReadResponse, await client.xread({key: last_id}, block=block_ms, count=10))
        if not response:
            continue  # no new entries within block_ms — poll again
        _, entries = response[0]
        for entry_id, fields in entries:
            last_id = entry_id
            event = json.loads(fields["payload"])
            yield event
            if event.get("type") in ("done", "error", "approval_required"):
                return


async def delete_results_stream(client: redis.Redis, request_id: str) -> None:
    """Best-effort cleanup once the producer has consumed a terminal event —
    not required for correctness (TTL already bounds a leaked stream's
    lifetime), just avoids waiting out the full TTL on the common path."""
    try:
        await client.delete(results_stream_key(request_id))
    except Exception as exc:  # noqa: BLE001 - cleanup is optional, never worth failing a turn over
        logger.warning("queue_results_cleanup_failed", extra={"error_class": type(exc).__name__})
