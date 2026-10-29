"""How a front door other than the HTTP API runs a turn: publish a job, read its events back.

The CLI (`app/channels/chat.py`) and the Telegram channel (`app/channels/telegram.py`) used to drive the graph
in their own process. They now do what `app/api/routers/chat.py` does: publish the turn onto the domain's
requests stream (`queue.py`) and read the events a worker publishes to the job's results stream. Every front
door therefore has one execution path, the worker's, and what the worker adds applies to all: the per-thread
lock (two messages into one conversation can't overlap), crash reclaim and the dead-letter archive, and pool
scaling. A front door holds no checkpointer pool, model client or tool runtime.

Three entry points: `stream_turn` and `stream_resume` (the answer to an `approval_required`) are async
generators of the same typed events `_run_graph_stream` yields; `cancel_turn` publishes a stop and reads
nothing back. All three take a `domain`, which MUST be the domain of the worker pool the conversation started
on (pattern 49): a resume or cancel landing on another domain's stream would run the wrong tool universe
against a paused graph.

Callers wrap the iterator in `contextlib.aclosing` when they can stop reading early (an `error` event, an
`approval_required` they hand to a person), as `.claude/rules/runtime-reliability.md` asks of every loop over
a turn's stream.

This module does NOT authorize. The API checks conversation ownership (`_require_conversation_owner`) before
publishing because its caller is a client. These callers derive their own `SecurityCtx` and `thread_id` in
their own trusted process (the OS user for the CLI, the Telegram user and chat for the channel), exactly as
they did when running the graph themselves, and the worker trusts the `ctx` on the job the same way for all
three.
"""
import hashlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

from app.core.config import CHAT_FIRST_RESPONSE_DEADLINE_SECONDS
from app.core.security import SecurityCtx
from app.job_queue.queue import (
    RESULTS_STREAM_TTL_SECONDS,
    claim_or_get_existing_submission,
    get_client,
    publish_cancel_request,
    publish_request,
    publish_resume_request,
    read_results,
    release_submission_claim,
    set_cancel_flag,
)


async def stream_turn(
    text: str,
    thread_id: str,
    ctx: SecurityCtx,
    *,
    domain: str,
    require_approval: bool = False,
    unattended: bool = False,
    dedup_key: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Publish one new turn and yield its events, ending at the terminal one.

    `unattended` asks the worker to decline an approval pause rather than report it (the Telegram channel
    can't answer one). Without it, a pause ends the stream at an `approval_required` event and the caller
    answers with `stream_resume`.

    `dedup_key` makes the submission idempotent per `(thread_id, dedup_key)`. A second call with the same key
    publishes nothing and reads the FIRST attempt's results stream instead
    (`queue.claim_or_get_existing_submission`). A channel that is redelivered its input needs this, and the
    queue is what makes it necessary: when the channel ran the graph itself, a crash took the turn down with
    it, so replaying the message was a retry; now the turn outlives the channel, and replaying would be a
    SECOND turn on the same conversation (a second model call, a second copy of the question in the thread, a
    second reply).

    The claim lasts as long as the results stream (`RESULTS_STREAM_TTL_SECONDS`, refreshed on every write, so
    it ends no sooner than the claim). A hit therefore finds either the first attempt's events or a job still
    waiting for a worker, never a stream that expired under a live claim. Without a key every call is a new
    turn.

    Like any async generator, nothing is awaited until the first `anext`, so a caller that needs the job
    published before doing anything else must start iterating first.
    """
    client = get_client()
    request_id = uuid.uuid4().hex
    digest = None if dedup_key is None else hashlib.sha256(dedup_key.encode()).hexdigest()
    is_new = True
    if digest is not None:
        request_id, is_new = await claim_or_get_existing_submission(
            client,
            thread_id=thread_id,
            digest=digest,
            request_id=request_id,
            ttl_seconds=RESULTS_STREAM_TTL_SECONDS,
        )
    if is_new:
        try:
            await publish_request(
                client,
                request_id=request_id,
                text=text,
                thread_id=thread_id,
                ctx=ctx,
                domain=domain,
                require_approval=require_approval,
                unattended=unattended,
            )
        except Exception:
            # The claim above already landed. Left as it is it points at a request_id no job
            # was published under, so a replay inside the TTL would read a stream nobody will
            # ever write to (see the same compensation in app/api/routers/chat.py).
            if digest is not None:
                await release_submission_claim(client, thread_id=thread_id, digest=digest)
            raise
    async with aclosing(_events(client, request_id)) as events:
        async for event in events:
            yield event


async def stream_resume(
    thread_id: str, approved: bool, ctx: SecurityCtx, *, domain: str
) -> AsyncGenerator[dict, None]:
    """Publish the answer to an `approval_required` and yield the events of the run it starts.

    Any worker in `domain`'s pool can take it: the paused graph is in the checkpointer, not in
    the process that began the turn. `ctx` is supplied again rather than remembered, the way
    `astream_events_resume` asks (identity does not persist across a resume).
    """
    client = get_client()
    request_id = uuid.uuid4().hex
    await publish_resume_request(
        client, request_id=request_id, thread_id=thread_id, approved=approved, ctx=ctx, domain=domain
    )
    async with aclosing(_events(client, request_id)) as events:
        async for event in events:
            yield event


async def cancel_turn(thread_id: str, ctx: SecurityCtx, *, domain: str) -> None:
    """Stop a conversation's turn without reading anything back: what `POST /chat/cancel` does.

    Both mechanisms fire and each is a no-op where it does not apply (see that endpoint): the
    flag stops a turn that is streaming, the job cancels one that is paused at an approval. A
    CLI that is interrupted needs this because the turn is no longer its own to kill — closing
    the process used to stop the turn, and now a worker would run it to the end, including a
    tool the person was about to be asked about.
    """
    client = get_client()
    await set_cancel_flag(client, thread_id)
    await publish_cancel_request(
        client, request_id=uuid.uuid4().hex, thread_id=thread_id, ctx=ctx, domain=domain
    )


def _events(client, request_id: str) -> AsyncGenerator[dict, None]:
    """One job's results, with the same wait for a first event the HTTP path has.

    Without the deadline a front door whose domain has no worker would wait forever; with it the
    person gets `read_results`'s one error event naming the problem ("is an agent-worker running
    for this domain?"), and `agent_worker_unreachable_total` counts it for the alert.
    """
    return read_results(
        client, request_id, first_event_deadline_seconds=CHAT_FIRST_RESPONSE_DEADLINE_SECONDS
    )
