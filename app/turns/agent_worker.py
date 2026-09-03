"""Redis Streams consumer — the "agent worker" half of the SSE-service/
agent-worker split (GRAPH_PATTERNS.md pattern 43). Run one or more of these
processes; Redis's own consumer-group delivery guarantees each request on
`app/turns/queue.py::REQUESTS_STREAM` is handed to exactly one of them, so running
more workers is still the primary way to add capacity — no coordination code
needed here beyond what `app/turns/queue.py` already wraps.

Within ONE process, `run()` also runs up to `_MAX_CONCURRENCY` turns at once
(an `asyncio.Semaphore`-bounded `asyncio.create_task` per job, not a serial
`await` loop) rather than one job at a time — safe because a turn holds no
in-process state a concurrent sibling could corrupt: conversation state
lives in Postgres via the checkpointer (see above), tenant-scoped resources
(Redis, Qdrant, the appdata pool) are already per-call/pooled, and
`app/core/logging_config.py`'s `bind_request_id` uses a `contextvars.ContextVar`,
which `asyncio.create_task` copies per-task rather than sharing — a request
id set inside one task's turn is invisible to a sibling task's log lines.
The bottleneck this doesn't remove: whatever's actually serving the LLM
calls downstream (see app/agent/runtime.py::_open_checkpointer's docstring
for the checkpointer's own remaining serialization point, a fine-grained
one — the point of `_MAX_CONCURRENCY` is to let the SLOW parts of several
turns, LLM calls and tool execution, actually overlap, not to guarantee
some fixed throughput multiplier).

Three job kinds share this one stream (`payload["kind"]`, default `"turn"`
for backward compatibility with any payload published before this field
existed):
- `"turn"` — a new turn. Runs `astream_events_turn` (NOT `_unattended`):
  this queued path is now the web UI's DEFAULT route to a real, watching
  browser (app/api/main.py's `POST /chat/stream/queued`), which can render a
  real approve/reject UI for an `approval_required` pause — auto-declining
  it the way `_unattended` does would make that impossible. Wired with a
  `cancel_check` polling `app/turns/queue.py::is_cancelled` so `POST /chat/cancel`
  can stop an actively-streaming (not yet paused) turn.
- `"resume"` — continues a turn paused at human_approval, via
  `astream_events_resume`. Any worker can handle any thread's resume: the
  checkpoint they all share lives in Postgres, not in this process's
  memory (app/agent/runtime.py's module docstring) — Redis's own consumer-group
  balancing is all the routing this needs.
- `"cancel"` — cancels a turn paused at human_approval, via `cancel_run`.
  A no-op (nothing was paused) if the target turn was actively streaming
  instead — that case is handled by the cancel-flag mechanism above, not
  this job kind (see `POST /chat/cancel`'s docstring in app/api/main.py).

For each job: publish every yielded event to that request's own results
stream, then acknowledge the request so it's never redelivered. A worker
process that dies mid-turn leaves its request unacknowledged — Redis keeps
it pending for the group, so a restarted (or different) worker can claim it
via `XCLAIM`/`XAUTOCLAIM`; not wired up here (see GRAPH_PATTERNS.md's
"Extending Further" note) to keep this module's first version focused on
the actually-requested capability: independent scaling, not full exactly-
once fault tolerance.

`astream_events_turn_unattended` (auto-declines any pause) is still used
by other genuinely fire-and-forget callers with no interactive human on
the other end of the SAME call — none in this codebase route through
THIS queue today, but the function stays available for one that might.

Run with: `python -m app.turns.agent_worker` (see Makefile's `agent-worker`
target). Needs `make up`'s Redis running; NOT started by `make up` itself
or `make serve` — it's an alternate, opt-in path alongside the existing
direct in-process `POST /chat/stream`, not a replacement for it (see
app/api/main.py's `POST /chat/stream/queued` for the producer half).
"""
import asyncio
import json
import logging
import signal
import socket
import uuid
from typing import cast

from app.agent import sql_store
from app.agent.runtime import (
    astream_events_resume,
    astream_events_turn,
    cancel_run,
    close_checkpointer_pool,
    init_graph_async,
)
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.logging_config import bind_request_id, configure_logging
from app.core.telemetry import configure_telemetry
from app.turns.queue import (
    CONSUMER_GROUP,
    REQUESTS_STREAM,
    StreamReadResponse,
    clear_cancel_flag,
    ensure_consumer_group,
    get_client,
    is_cancelled,
    publish_result,
)

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_MAX_CONCURRENCY = 10  # concurrent turns ONE worker process will run at once —
# bounds both the asyncio.Semaphore in run() below and how many entries a
# single xreadgroup call pulls off the stream, so this process never claims
# more work than it's currently willing to start (an unclaimed entry just
# stays pending for the group — another worker, or this one on its next
# read, can still pick it up).
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
    """Cancels a PAUSED turn only — see this module's docstring for why an
    actively-streaming one is a no-op here (`cancel_run` correctly reports
    "nothing to cancel", the same result it'd give for a thread that was
    never paused at all)."""
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
    always ack, even on failure: a request this worker already attempted
    (and reported an error event for) must not be silently redelivered to
    another worker and attempted again, which would run the same
    side-effecting tool calls twice."""
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
    failure — `process_request` already acks in its own `finally` regardless
    of outcome, so the only thing this wrapper owns is the concurrency
    slot."""
    try:
        await process_request(client, entry_id, fields)
    finally:
        semaphore.release()


async def run() -> None:
    await init_graph_async()  # opens the durable checkpointer on THIS process's own loop
    client = get_client()
    await ensure_consumer_group(client)
    logger.info(
        "agent_worker_started",
        extra={"consumer": CONSUMER_NAME, "max_concurrency": _MAX_CONCURRENCY},
    )

    # Graceful shutdown: a SIGTERM/SIGINT (docker stop, a process supervisor
    # restarting this worker, Ctrl-C) sets this instead of killing the loop
    # mid-read. Checked only BETWEEN xreadgroup calls, never inside the
    # entries loop below — so every job already claimed by this worker (up
    # to `_MAX_CONCURRENCY` of them, running concurrently — see this
    # module's own docstring) always runs to completion and gets acked
    # before exit, rather than being abandoned mid-turn the way an
    # unhandled kill signal would (compounding with the lack of
    # XCLAIM/XAUTOCLAIM redelivery this module's own docstring already
    # flags — a job dropped THAT way has no automatic recovery at all).
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Bounds how many jobs this ONE process runs at once. Acquired in the
    # read loop below, BEFORE a task is even created — not inside the task
    # — so a full semaphore also backpressures reading: this process simply
    # stops pulling new entries off the stream once it's at capacity,
    # leaving them pending for the group (another worker, or this one once
    # a slot frees up, can still claim them) rather than piling up an
    # unbounded number of not-yet-running tasks in `in_flight` below.
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
    # Same reasoning as app/api/main.py's lifespan shutdown: a turn this worker
    # ran may have opened app/agent/sql_store.py's connection pool (query_employees,
    # app/agent/meter.py::record_usage) — leaving it open past process exit is
    # what produces the "couldn't stop thread... within 5.0 seconds" warning
    # documented there. A no-op if this worker never touched it.
    sql_store.close_pool()
    # Same reasoning for the checkpointer's own pool (app/agent/runtime.py) —
    # init_graph_async() above always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-agent-worker")
    asyncio.run(run())
