"""Redis Streams consumer — the "agent worker" half of the SSE-service/
agent-worker split (GRAPH_PATTERNS.md pattern 43). Run one or more of these
processes; Redis's own consumer-group delivery guarantees each request on
`app/queue.py::REQUESTS_STREAM` is handed to exactly one of them, so running
more workers is the whole scaling story — no coordination code needed here
beyond what `app/queue.py` already wraps.

Three job kinds share this one stream (`payload["kind"]`, default `"turn"`
for backward compatibility with any payload published before this field
existed):
- `"turn"` — a new turn. Runs `astream_events_turn` (NOT `_unattended`):
  this queued path is now the web UI's DEFAULT route to a real, watching
  browser (app/api.py's `POST /chat/stream/queued`), which can render a
  real approve/reject UI for an `approval_required` pause — auto-declining
  it the way `_unattended` does would make that impossible. Wired with a
  `cancel_check` polling `app/queue.py::is_cancelled` so `POST /chat/cancel`
  can stop an actively-streaming (not yet paused) turn.
- `"resume"` — continues a turn paused at human_approval, via
  `astream_events_resume`. Any worker can handle any thread's resume: the
  checkpoint they all share lives in Postgres, not in this process's
  memory (app/agent.py's module docstring) — Redis's own consumer-group
  balancing is all the routing this needs.
- `"cancel"` — cancels a turn paused at human_approval, via `cancel_run`.
  A no-op (nothing was paused) if the target turn was actively streaming
  instead — that case is handled by the cancel-flag mechanism above, not
  this job kind (see `POST /chat/cancel`'s docstring in app/api.py).

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

Run with: `python -m app.agent_worker` (see Makefile's `agent-worker`
target). Needs `make up`'s Redis running; NOT started by `make up` itself
or `make serve` — it's an alternate, opt-in path alongside the existing
direct in-process `POST /chat/stream`, not a replacement for it (see
app/api.py's `POST /chat/stream/queued` for the producer half).
"""
import asyncio
import json
import logging
import socket
import uuid

from app.agent import astream_events_resume, astream_events_turn, cancel_run, init_graph_async
from app.errors import ErrorCode, ErrorEnvelope
from app.queue import (
    CONSUMER_GROUP,
    REQUESTS_STREAM,
    clear_cancel_flag,
    ensure_consumer_group,
    get_client,
    is_cancelled,
    publish_result,
)

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_READ_COUNT = 1  # one turn at a time per worker — a turn can run for many seconds
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


async def run() -> None:
    await init_graph_async()  # opens the durable checkpointer on THIS process's own loop
    client = get_client()
    await ensure_consumer_group(client)
    logger.info("agent_worker_started", extra={"consumer": CONSUMER_NAME})
    while True:
        response = await client.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {REQUESTS_STREAM: ">"},
            count=_READ_COUNT,
            block=_BLOCK_MS,
        )
        if not response:
            continue
        _, entries = response[0]
        for entry_id, fields in entries:
            await process_request(client, entry_id, fields)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
