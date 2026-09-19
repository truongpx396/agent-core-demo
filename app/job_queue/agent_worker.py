"""Redis Streams consumer — the "agent worker" half of the SSE-service/
agent-worker split (GRAPH_PATTERNS.md pattern 43). Run one or more of these;
Redis's consumer-group delivery hands each request on this process's domain
stream (`app/job_queue/queue.py::requests_stream_key`) to exactly one of
them, so running more workers is how you add capacity.

Like `app/channels/telegram.py`, ONE process serves exactly ONE domain for
its life — `AGENT_DOMAIN` (default `"ecorp"`) picks which domain's
manifest/tools the graph is built from and which requests stream is read
(`AGENT_DOMAIN=support python -m app.job_queue.agent_worker` runs a
support-only pool). Running several domains means running several worker
POOLS; `app/api/main.py` stays one unified process, routing by `X-Domain`.

`run()` runs up to `_MAX_CONCURRENCY` turns at once per process
(`asyncio.Semaphore`-bounded `create_task` per job). Safe because a turn
holds no in-process state a sibling could corrupt: conversation state
lives in Postgres via the checkpointer, tenant-scoped resources are
per-call/pooled, and `bind_request_id`'s `ContextVar` is copied per-task by
`asyncio.create_task`, not shared. `_MAX_CONCURRENCY` lets the slow parts
(LLM calls, tool execution) of several turns overlap — it doesn't
eliminate the checkpointer's own finer-grained serialization point (see
`app/agent/runtime.py::_open_checkpointer`).

Three job kinds share this stream (`payload["kind"]`, default `"turn"` for
backward compatibility):
- `"turn"` — a new turn, via `astream_events_turn` (not `_unattended`):
  this is the web UI's default route to a real watching browser
  (`POST /chat/stream/queued`), which can render an approve/reject UI for
  an `approval_required` pause. Wired with `cancel_check` (polls
  `queue.py::is_cancelled`) so `POST /chat/cancel` can stop an
  actively-streaming turn.
- `"resume"` — continues a turn paused at human_approval via
  `astream_events_resume`. Any worker in the pool can handle it; the
  checkpoint lives in Postgres, not worker memory.
- `"cancel"` — cancels a turn paused at human_approval via `cancel_run`;
  a no-op if the turn was actively streaming instead (that's the
  cancel-flag mechanism, see `POST /chat/cancel` in app/api/main.py).

For each job: publish every yielded event to the request's results stream,
then ack so it's never redelivered. A worker that dies mid-turn leaves its
request unacknowledged (pending for the group, reclaimable via
XCLAIM/XAUTOCLAIM) — not wired up here; this module targets independent
scaling, not full exactly-once fault tolerance.

`astream_events_turn_unattended` (auto-declines any pause) stays available
for a genuinely fire-and-forget caller; nothing in this codebase routes
through this queue today.

Run with: `python -m app.job_queue.agent_worker` (Makefile's `agent-worker`
target, or `agent-worker-support`/`-ops`/`-sales`). Needs `make up`'s
Redis; not started by `make up`/`make serve` — an opt-in path alongside the
direct in-process `POST /chat/stream` (producer half:
`POST /chat/stream/queued`).
"""
import asyncio
import json
import logging
import signal
import socket
import uuid
from typing import cast

from app.agent import sql_store
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.agent.runtime_stream import (
    astream_events_resume,
    astream_events_turn,
    cancel_run,
)
from app.core.config import AGENT_DOMAIN, AGENT_WORKER_MAX_CONCURRENCY
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.logging_config import bind_request_id, configure_logging
from app.core.telemetry import configure_telemetry
from app.domains.registry import resolve_domain
from app.job_queue.queue import (
    CONSUMER_GROUP,
    StreamReadResponse,
    clear_cancel_flag,
    ensure_consumer_group,
    get_client,
    is_cancelled,
    publish_result,
    requests_stream_key,
)

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
# This process's own domain's requests stream, fixed for its whole life
# (domain is a per-PROCESS property, not per-message — see module docstring).
REQUESTS_STREAM = requests_stream_key(AGENT_DOMAIN)
_MAX_CONCURRENCY = AGENT_WORKER_MAX_CONCURRENCY  # concurrent turns per worker
# process — bounds both the semaphore in run() and how many entries one
# xreadgroup call pulls, so this process never claims more than it's
# willing to start (unclaimed entries stay pending for the group).
_READ_COUNT = _MAX_CONCURRENCY
_BLOCK_MS = 5000


async def _process_turn(client, request_id: str, payload: dict) -> None:
    thread_id = payload["thread_id"]
    # A stale flag from an EARLIER turn on this thread_id (e.g. /chat/cancel
    # raced with that turn already finishing on its own) must not
    # spuriously cancel this brand-new one.
    await clear_cancel_flag(client, thread_id)

    async def cancel_check() -> bool:
        return await is_cancelled(client, thread_id)

    async for event in astream_events_turn(
        payload["text"],
        thread_id,
        payload["ctx"],
        require_approval=payload.get("require_approval", False),
        images=payload.get("images") or None,
        cancel_check=cancel_check,
    ):
        await publish_result(client, request_id, event)


async def _process_resume(client, request_id: str, payload: dict) -> None:
    async for event in astream_events_resume(
        payload["thread_id"], payload["approved"], payload["ctx"]
    ):
        await publish_result(client, request_id, event)


async def _process_cancel(client, request_id: str, payload: dict) -> None:
    """Cancels a PAUSED turn only — an actively-streaming one is a no-op
    here (`cancel_run` reports "nothing to cancel", same as a thread never
    paused at all; see module docstring)."""
    cancelled = await cancel_run(payload["thread_id"], payload["ctx"])
    if cancelled:
        envelope = ErrorEnvelope(code=ErrorCode.CANCELLED, message="Cancelled by user.")
        event = {"type": "error", "content": envelope.message, **envelope.to_dict()}
    else:
        event = {"type": "done"}
    await publish_result(client, request_id, event)


_DISPATCH = {"turn": _process_turn, "resume": _process_resume, "cancel": _process_cancel}


async def process_request(client, entry_id: str, fields: dict) -> None:
    """Dispatch one job by `payload["kind"]` and stream its events back —
    always ack, even on failure: a redelivered, already-attempted request
    would re-run the same side-effecting tool calls twice."""
    payload = json.loads(fields["payload"])
    request_id = payload["request_id"]
    kind = payload.get("kind", "turn")
    handler = _DISPATCH.get(kind)
    with bind_request_id(request_id):
        try:
            if handler is None:
                raise ValueError(f"unknown job kind: {kind!r}")
            await handler(client, request_id, payload)
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            logger.warning(
                "agent_worker_turn_failed",
                extra={"request_id": request_id, "kind": kind, "error_class": type(exc).__name__},
            )
            await publish_result(client, request_id, {"type": "error", "content": str(exc)})
        finally:
            await client.xack(REQUESTS_STREAM, CONSUMER_GROUP, entry_id)


async def _process_with_limit(
    client, entry_id: str, fields: dict, semaphore: asyncio.Semaphore
) -> None:
    """Runs one job under `semaphore` and releases it when done, success or
    failure — `process_request` already acks regardless of outcome, so this
    wrapper only owns the concurrency slot."""
    try:
        await process_request(client, entry_id, fields)
    finally:
        semaphore.release()


async def run() -> None:
    manifest, domain = resolve_domain(AGENT_DOMAIN)  # fails loud on a typo'd AGENT_DOMAIN
    await init_graph_async(manifest=manifest, domain=domain)  # opens the durable
    # checkpointer on THIS process's own loop, against AGENT_DOMAIN's manifest/tools
    client = get_client()
    await ensure_consumer_group(client, AGENT_DOMAIN)
    logger.info(
        "agent_worker_started",
        extra={"consumer": CONSUMER_NAME, "domain": manifest.name, "max_concurrency": _MAX_CONCURRENCY},
    )

    # Graceful shutdown: SIGTERM/SIGINT sets this instead of killing the
    # loop mid-read. Checked only BETWEEN xreadgroup calls, never inside
    # the entries loop, so every already-claimed job runs to completion and
    # gets acked before exit (there's no XCLAIM/XAUTOCLAIM redelivery here —
    # see module docstring — so an abandoned job has no automatic recovery).
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Bounds jobs this process runs at once. Acquired BEFORE the task is
    # created, so a full semaphore also backpressures reading: entries stay
    # pending for the group instead of piling up unbounded in `in_flight`.
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    in_flight: set[asyncio.Task] = set()

    while not stop_event.is_set():
        response = cast(
            StreamReadResponse,
            await client.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {REQUESTS_STREAM: ">"},
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
        "agent_worker_stopping", extra={"consumer": CONSUMER_NAME, "in_flight": len(in_flight)}
    )
    # Let every already-claimed job finish (and ack) instead of abandoning
    # it mid-turn — see the graceful-shutdown comment above.
    if in_flight:
        await asyncio.gather(*in_flight)
    await client.aclose()
    # Same reasoning as app/api/main.py's lifespan shutdown: a turn may have
    # opened sql_store.py's connection pool; leaving it open past exit
    # produces the "couldn't stop thread... within 5.0 seconds" warning
    # documented there. No-op if this worker never touched it.
    await sql_store.close_pool()
    # Same reasoning for the checkpointer's pool — init_graph_async() above
    # always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-agent-worker")
    asyncio.run(run())
