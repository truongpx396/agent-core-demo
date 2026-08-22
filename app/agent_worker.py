"""Redis Streams consumer — the "agent worker" half of the SSE-service/
agent-worker split (GRAPH_PATTERNS.md pattern 43). Run one or more of these
processes; Redis's own consumer-group delivery guarantees each request on
`app/queue.py::REQUESTS_STREAM` is handed to exactly one of them, so running
more workers is the whole scaling story — no coordination code needed here
beyond what `app/queue.py` already wraps.

For each request: run the graph via `astream_events_turn_unattended`
(app/agent.py) and publish each yielded event to that request's own results
stream, then acknowledge the request so it's never redelivered. A worker
process that dies mid-turn leaves its request unacknowledged — Redis keeps
it pending for the group, so a restarted (or different) worker can claim it
via `XCLAIM`/`XAUTOCLAIM`; not wired up here (see GRAPH_PATTERNS.md's
"Extending Further" note) to keep this module's first version focused on
the actually-requested capability: independent scaling, not full exactly-
once fault tolerance.

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

from app.agent import astream_events_turn_unattended, init_graph_async
from app.queue import (
    CONSUMER_GROUP,
    REQUESTS_STREAM,
    ensure_consumer_group,
    get_client,
    publish_result,
)

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_READ_COUNT = 1  # one turn at a time per worker — a turn can run for many seconds
_BLOCK_MS = 5000


async def process_request(client, entry_id: str, fields: dict) -> None:
    """Run one turn and stream its events back — always ack, even on
    failure: a request this worker already attempted (and reported an
    error event for) must not be silently redelivered to another worker
    and attempted again, which would run the same side-effecting tool
    calls twice."""
    payload = json.loads(fields["payload"])
    request_id = payload["request_id"]
    try:
        async for event in astream_events_turn_unattended(
            payload["text"],
            payload["thread_id"],
            payload["ctx"],
            require_approval=payload.get("require_approval", False),
            images=payload.get("images") or None,
        ):
            await publish_result(client, request_id, event)
    except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
        logger.warning(
            "agent_worker_turn_failed",
            extra={"request_id": request_id, "error_class": type(exc).__name__},
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
