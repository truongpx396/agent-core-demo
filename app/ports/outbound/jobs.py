"""The agent's work queue as the API sees it, and the names the API and the workers must agree on.

A turn is a job on a per-domain requests stream; its events come back on a per-request results stream. `TurnQueue` is what the API does with that:
submit a turn, a resume or a cancel, flag a running turn to stop, read the events back. `IngestQueue` is the same for an upload.

Reading the requests stream as a worker (the consumer group, acknowledging, reclaiming a dead worker's jobs, the dead-letter stream) is the workers'
own Streams technology, not a port: nothing in the application calls it (`app/adapters/inbound/workers/streams.py`). What both halves must agree on is
plain data and lives here: stream and key names, TTLs, which events end a stream. The Redis adapters are `app/adapters/outbound/redis/queue.py` and
`ingest_queue.py`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.config import REQUEST_TIMEOUT_SECONDS
from app.core.security import SecurityCtx
from app.ports.slot import Slot

CONSUMER_GROUP = "agent-workers"
# The last event type a turn ever publishes: the reader stops at one, and the worker releases the thread lock the moment it publishes one.
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "approval_required"})
RESULTS_STREAM_TTL_SECONDS = 300  # an abandoned results stream, chat or ingest, expires after 5 minutes
CANCEL_FLAG_TTL_SECONDS = 60  # outlasts the gap between a worker's cancel checks; short, so a flag nobody consumed cannot cancel a later turn
# A safety net, not the release path (the worker always releases): outlives the longest turn, so a worker that dies mid-turn cannot wedge its thread.
THREAD_LOCK_TTL_SECONDS = REQUEST_TIMEOUT_SECONDS * 2

INGEST_REQUESTS_STREAM = "ingest:requests"
INGEST_CONSUMER_GROUP = "ingest-workers"


def results_stream_key(request_id: str) -> str:
    return f"agent:results:{request_id}"


def requests_stream_key(domain: str = "ecorp") -> str:
    """The one requests stream the workers serving `domain` (`AGENT_WORKER_DOMAINS`) all
    consume from as a single consumer group — see module docstring for why
    domain-scoping keeps each domain's load independent. Defaults to
    `"ecorp"`, same convention as `app/agent/runtime/core.py::DEFAULT_DOMAIN`."""
    return f"agent:requests:{domain}"


def cancel_flag_key(thread_id: str) -> str:
    return f"agent:cancel:{thread_id}"


def thread_lock_key(thread_id: str) -> str:
    return f"agent:lock:{thread_id}"


def ingest_results_stream_key(job_id: str) -> str:
    return f"ingest:results:{job_id}"


class TurnQueue(Protocol):
    async def claim_or_get_existing_submission(self, *, thread_id: str, digest: str, request_id: str, ttl_seconds: int) -> tuple[str, bool]:
        """Claims `digest` for `thread_id` for `ttl_seconds`. Returns `(request_id to use, is_new)`: an identical submission inside the window gets the
        first attempt's id and must not publish a second job, so a client's retry streams the same turn instead of running it twice."""
        ...

    async def release_submission_claim(self, *, thread_id: str, digest: str) -> None:
        """Gives back a claim whose job then failed to publish. Best effort: never raises."""
        ...

    async def publish_request(
        self,
        *,
        request_id: str,
        text: str,
        thread_id: str,
        ctx: SecurityCtx,
        domain: str = "ecorp",
        require_approval: bool = False,
        images: list[str] | None = None,
        unattended: bool = False,
    ) -> None:
        """Enqueues a new turn on `domain`'s requests stream; only a worker serving that domain reads it. The caller makes `request_id`, so it can listen
        before publishing. `unattended` is a property of the job and survives a reclaim: nobody is there to approve."""
        ...

    async def publish_resume_request(self, *, request_id: str, thread_id: str, approved: bool, ctx: SecurityCtx, domain: str = "ecorp") -> None:
        """Enqueues the approve-or-reject decision for a turn paused at human approval. `domain` must be the original turn's."""
        ...

    async def publish_cancel_request(self, *, request_id: str, thread_id: str, ctx: SecurityCtx, domain: str = "ecorp") -> None:
        """Enqueues a cancel for a turn paused at human approval (an actively streaming turn is stopped by `set_cancel_flag`)."""
        ...

    async def set_cancel_flag(self, thread_id: str) -> None:
        """Flags the turn streaming on `thread_id` to stop at its next check. Short-lived."""
        ...

    def read_results(self, request_id: str, *, first_event_deadline_seconds: float | None = None) -> AsyncIterator[dict]:
        """Yields each event of `request_id`'s turn, in order, until a terminal one. `first_event_deadline_seconds` bounds only the wait for the FIRST
        event: if nobody ever publishes, it yields one `error` event and ends instead of hanging."""
        ...

    async def ping(self) -> None:
        """Raises if the queue's server is unreachable (the readiness probe)."""
        ...


class IngestQueue(Protocol):
    async def publish_ingest_request(
        self, *, job_id: str, object_key: str, filename: str, content_type: str, ctx: SecurityCtx, topic: str | None = None
    ) -> None:
        """Enqueues one ingest job. `object_key` points at the uploaded file in the object store; the job never carries the bytes."""
        ...

    def read_results(self, job_id: str, *, first_event_deadline_seconds: float | None = None) -> AsyncIterator[dict]:
        """Yields each event of `job_id`, in order, until `done` or `error`; same first-event deadline as `TurnQueue.read_results`."""
        ...

    async def delete_results_stream(self, job_id: str) -> None:
        """Best-effort cleanup once the reader has seen the end. Never raises."""
        ...


turn_queue: Slot[TurnQueue] = Slot("turn_queue", TurnQueue)
ingest_queue: Slot[IngestQueue] = Slot("ingest_queue", IngestQueue)
