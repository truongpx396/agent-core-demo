"""Redis Streams consumer: the "agent worker" half of the SSE-service/agent-worker split (GRAPH_PATTERNS.md
pattern 43). Run one or more; consumer-group delivery hands each request on this process's domain stream
(`queue.py::requests_stream_key`) to exactly one worker, so more workers means more capacity.

Like `app/channels/telegram.py`, ONE process serves exactly ONE domain for its life: `AGENT_DOMAIN` (default
`"ecorp"`) picks which manifest/tools the graph is built from and which stream is read (`AGENT_DOMAIN=support
python -m app.job_queue.agent_worker` runs a support-only pool). Several domains means several worker POOLS;
`app/api/main.py` stays one unified process, routing by `X-Domain`.

## Concurrency
`run()` runs up to `_MAX_CONCURRENCY` turns at once per process (`asyncio.Semaphore`-bounded `create_task` per
job). Different thread_ids are safe because a turn holds no in-process state a sibling could corrupt:
conversation state lives in Postgres via the checkpointer, tenant-scoped resources are per-call/pooled, and
`bind_request_id`'s `ContextVar` is copied per task. The slow parts (LLM calls, tools) overlap; the
checkpointer's own finer-grained serialization point remains
(`app/agent/runtime/core.py::_open_checkpointer`).

The SAME thread_id is different: two jobs would both read the latest checkpoint as their parent and both write
a child, so the last commit silently wins. `process_request` therefore takes `queue.py::acquire_thread_lock`
before any job (across this process's tasks AND other replicas sharing the Redis) and rejects the loser with
`ErrorCode.THREAD_BUSY`.

## Job kinds
Four share this stream (`payload["kind"]`, default `"turn"` for backward compatibility). A real producer
(`queue.py::publish_request`/`publish_resume_request`/`publish_cancel_request`) creates only the first three;
`"turn_continue"` comes only from this module's own reclaim path.
- `"turn"`: a new turn via `astream_events_turn` (not `_unattended`), the web UI's route to a watching browser
  (`POST /chat/stream/queued`) that can render an `approval_required` pause. Wired with `cancel_check` (polls
  `queue.py::is_cancelled`) so `POST /chat/cancel` can stop it mid-stream.
- `"resume"`: continues a turn paused at human_approval via `astream_events_resume`. Any worker can take it;
  the checkpoint is in Postgres. Wired with the same `cancel_check`, so cancel also stops the run an approval
  started (the approved tool, then the model call reading its result); a flag left from the earlier streaming
  phase is cleared first.
- `"cancel"`: cancels a turn paused at human_approval via `cancel_run`; a no-op if the turn was actively
  streaming (that is the cancel-flag path, `POST /chat/cancel` in app/api/routers/chat.py).
- `"turn_continue"`: a crashed `"turn"` reclaimed mid-flight, via `astream_events_continue_turn`
  (`graph.astream_events(None, ...)` instead of a fresh `{"messages": [...]}`). See that function's docstring
  for why this, not a second `astream_events_turn`, makes the retry safe.

## Ack and crash reclaim
Each job publishes every yielded event to the request's results stream, then acks so it is never redelivered.
A worker that dies before the ack (crash, OOM, host failure) leaves the request pending forever;
`_reclaim_loop` (run beside the read loop by every worker) sweeps for those via
`queue.py::reclaim_stale_entries` (XAUTOCLAIM) and hands each to `_handle_reclaimed_job`, which decides PER
JOB whether re-running is provably safe and how:
- `"cancel"`: always safe; cancelling is idempotent (it still applies, or there is nothing left to cancel).
- `"resume"`: also always safe since `app/core/tool_idempotency.py` guards every `mutating`/`outward` tool. A
  resume continues an EXISTING checkpoint from its `human_approval` pause and re-invokes the pending calls
  under the exact SAME `tool_call_id`s (the pause happens before they run), so any that already completed
  return their cached result. Before that guard existed, "resume" was excluded here (no fresh `HumanMessage`
  boundary to tell whether the just-approved call ran); tool-level idempotency closed that gap, not anything
  in this function.
- `"turn"`/`"turn_continue"`: classified by `_classify_reclaimed_turn` against the checkpointed state since
  the last `HumanMessage`:
  - `"dead_letter"` if the checkpoint can't be read; if that HumanMessage's turn already produced a final
    answer (`_turn_already_completed`; it can finish with usage/cost recorded via
    `runtime/stream.py::_record_turn_metrics` and ZERO tool calls, so a retry would only double-record the
    cost); or if it crashed between a real `human_approval` interrupt and this job's ack (a healthy worker
    acks a job that reaches a pause like one that finishes, so a reclaimed `"turn"` finding one means the
    crash landed in that sliver).
  - `"retry_fresh"` (republished as `"turn"` with its original text) only if NO `HumanMessage` exists yet for
    the thread: the crash came before anything was checkpointed.
  - `"retry_continue"` (republished as `"turn_continue"`) otherwise: the turn's own `HumanMessage` is
    checkpointed and unfinished. Safe EVEN IF a mutating/outward tool already completed, with no per-tool
    capability check, because `astream_events_continue_turn` continues the exact Pregel run LangGraph has
    checkpointed, which never re-executes a task whose write is recorded (mechanics in that function's
    docstring). A fresh `astream_events_turn` mints new `tool_call_id`s unrelated to the crashed attempt's,
    which is what used to make retrying a turn that ran a mutating tool unsafe.
- Every non-`dead_letter` case is silently republished via `queue.py::republish_job`, up to
  `MAX_AUTO_RECLAIM_RETRIES` attempts (`queue.py::RECLAIM_ATTEMPTS_FIELD`). Beyond that, or on `dead_letter`,
  a `WORKER_LOST` error goes on the job's results stream and the job is archived to a dead-letter stream
  (`queue.py::dead_letter_stream_key`) for inspection or manual replay.

## Who publishes
The HTTP API (`POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel`), the CLI (`app/channels/chat.py`)
and the Telegram channel (`app/channels/telegram.py`), the last two through `app/job_queue/producer.py`. No
front door runs the graph in its own process. A `"turn"` carries `payload["unattended"]` when its caller has
nobody to show an approval to (Telegram): `_process_turn` then drives `astream_events_turn_unattended`
(auto-declines any pause, never approves), and `_process_turn_continue` does the same with its unattended
counterpart, so the flag survives a reclaim.

Run with `python -m app.job_queue.agent_worker` (Makefile `agent-worker`, or
`agent-worker-support`/`-ops`/`-sales`). Needs `make up`'s Redis; not started by `make up`/`make serve`. Every
chat front door, `make chat` and `make telegram` included, needs a worker running for its domain, or its turn
gets the "is an agent-worker running for this domain?" error.
"""
import asyncio
import json
import logging
import signal
import socket
import uuid
from contextlib import aclosing
from typing import cast

from langchain_core.messages import AIMessage

from app.agent.graph.messages import is_retry_artifact, is_user_message
from app.agent.runtime.core import close_checkpointer_pool, init_graph_async
from app.agent.runtime.stream import (
    astream_events_continue_turn,
    astream_events_continue_turn_unattended,
    astream_events_resume,
    astream_events_turn,
    astream_events_turn_unattended,
    cancel_run,
)
from app.core import metrics, sql_store
from app.core.config import (
    AGENT_DOMAIN,
    AGENT_WORKER_MAX_CONCURRENCY,
    AGENT_WORKER_RECLAIM_IDLE_SECONDS,
    MAX_AUTO_RECLAIM_RETRIES,
    WORKER_RECLAIM_INTERVAL_SECONDS,
)
from app.core.errors import (
    ErrorCode,
    ErrorEnvelope,
    error_origin,
    internal_error_envelope,
)
from app.core.logging_config import bind_request_id, configure_logging
from app.core.telemetry import configure_telemetry
from app.domains.registry import resolve_domain
from app.job_queue.queue import (
    CONSUMER_GROUP,
    RECLAIM_ATTEMPTS_FIELD,
    TERMINAL_EVENT_TYPES,
    StreamReadResponse,
    acquire_thread_lock,
    clear_cancel_flag,
    ensure_consumer_group,
    get_client,
    is_cancelled,
    publish_dead_letter,
    publish_result,
    reclaim_stale_entries,
    release_thread_lock,
    republish_job,
    requests_stream_key,
)
from app.mcp.connection import close_all_connections

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


async def _publish_events(client, request_id: str, events, release_lock) -> None:
    """Publishes a turn's events to its results stream, releasing the thread lock just before the terminal one.

    The stream is closed on EVERY exit, with `aclosing`: a bare `async for` does not close the
    generator when its loop is left by an exception (`publish_result` failing on a Redis blip, the
    task being cancelled while it awaits the publish), so the stream's own cleanup (finishing the
    trace, recording the turn as abandoned, releasing the budget hold) would run a loop turn
    AFTER this function's caller had already handled the failure. With it, that cleanup is done
    before the failure is seen, and does not depend on the event loop's finalizer hook.
    """
    async with aclosing(events) as stream:
        async for event in stream:
            if event.get("type") in TERMINAL_EVENT_TYPES:
                # Released BEFORE publishing, not after: publish_result itself
                # awaits a SECOND Redis round trip after its xadd (a TTL
                # refresh via `expire`) before returning — a reader polling via
                # `xread` can already see the xadd'd event and act on it (e.g.
                # POST /chat/resume fired the moment a client observes
                # approval_required) while that second round trip, and this
                # generator's own remaining cleanup, are still in flight.
                # Releasing first closes the window completely: by the time
                # ANY client could possibly observe this event, the lock is
                # already free. See process_request's `release_lock` docstring
                # for the real failure rate each ordering measured at.
                await release_lock()
            await publish_result(client, request_id, event)


async def _process_turn(client, request_id: str, payload: dict, release_lock) -> None:
    thread_id = payload["thread_id"]
    # A stale flag from an EARLIER turn on this thread_id (e.g. /chat/cancel
    # raced with that turn already finishing on its own) must not
    # spuriously cancel this brand-new one.
    await clear_cancel_flag(client, thread_id)

    async def cancel_check() -> bool:
        return await is_cancelled(client, thread_id)

    if payload.get("unattended"):
        # A caller with nobody to show an approval to (the Telegram channel): a pause is
        # declined here, so the channel only ever sees a finished turn and the conversation is
        # never left paused (constitution Principle II). No `cancel_check`: no HTTP caller can
        # name an unattended thread, so there is no /chat/cancel to poll for.
        events = astream_events_turn_unattended(
            payload["text"],
            thread_id,
            payload["ctx"],
            require_approval=payload.get("require_approval", False),
            images=payload.get("images") or None,
        )
    else:
        events = astream_events_turn(
            payload["text"],
            thread_id,
            payload["ctx"],
            require_approval=payload.get("require_approval", False),
            images=payload.get("images") or None,
            cancel_check=cancel_check,
        )
    await _publish_events(client, request_id, events, release_lock)


async def _process_turn_continue(client, request_id: str, payload: dict, release_lock) -> None:
    """Reclaim-only counterpart to `_process_turn`: same payload shape (the
    original `"turn"` job's, `kind` flipped by
    `_handle_reclaimed_job`/`_classify_reclaimed_turn`), but drives
    `astream_events_continue_turn` instead of `astream_events_turn` — see
    that function's own docstring for why continuing, not restarting, is
    what makes this retry safe. `text`/`images`/`require_approval` on the
    payload go unused here (the graph already has this turn's own
    `HumanMessage` checkpointed); kept on the payload rather than stripped,
    since `queue.py::republish_job` mutates only `kind`/the retry counter."""
    thread_id = payload["thread_id"]
    await clear_cancel_flag(client, thread_id)  # see _process_turn's own comment on this

    async def cancel_check() -> bool:
        return await is_cancelled(client, thread_id)

    if payload.get("unattended"):
        # The retry of an unattended turn is unattended too: `republish_job` keeps the flag,
        # and a continuation that reached an approval gate would otherwise end the job at an
        # `approval_required` its channel cannot answer.
        events = astream_events_continue_turn_unattended(thread_id, payload["ctx"])
    else:
        events = astream_events_continue_turn(thread_id, payload["ctx"], cancel_check=cancel_check)
    await _publish_events(client, request_id, events, release_lock)


async def _process_resume(client, request_id: str, payload: dict, release_lock) -> None:
    thread_id = payload["thread_id"]
    # Same reasoning as `_process_turn`: a flag left from the streaming phase
    # that led to this pause (a /chat/cancel that raced the turn pausing on
    # its own) must not cancel the approval the user has since given — the
    # later action is the user's intent. A cancel pressed after this point is
    # set after the clear and is seen by the polling below.
    await clear_cancel_flag(client, thread_id)

    async def cancel_check() -> bool:
        return await is_cancelled(client, thread_id)

    await _publish_events(
        client,
        request_id,
        astream_events_resume(thread_id, payload["approved"], payload["ctx"], cancel_check=cancel_check),
        release_lock,
    )


async def _process_cancel(client, request_id: str, payload: dict, release_lock) -> None:
    """Cancels a PAUSED turn only — an actively-streaming one is a no-op
    here (`cancel_run` reports "nothing to cancel", same as a thread never
    paused at all; see module docstring)."""
    cancelled = await cancel_run(payload["thread_id"], payload["ctx"])
    if cancelled:
        envelope = ErrorEnvelope(code=ErrorCode.CANCELLED, message="Cancelled by user.")
        event = {"type": "error", "content": envelope.message, **envelope.to_dict()}
    else:
        event = {"type": "done"}
    await release_lock()  # single-shot job, always terminal — see _publish_events's comment
    await publish_result(client, request_id, event)


_DISPATCH = {
    "turn": _process_turn,
    "turn_continue": _process_turn_continue,
    "resume": _process_resume,
    "cancel": _process_cancel,
}


async def process_request(client, entry_id: str, fields: dict) -> None:
    """Dispatch one job by `payload["kind"]` and stream its events back. Always ack, even on failure: a
    redelivered, already-attempted request would re-run the same side-effecting tool calls.

    Every job kind first acquires `queue.py::acquire_thread_lock` for its `thread_id`. The module docstring
    notes a turn holds no IN-PROCESS state a sibling could corrupt, which is true of this process's memory but
    not of the shared Postgres checkpointer: two jobs on the SAME thread_id (a double-submit, a retry, or a
    resume racing a fresh turn, possibly on different worker replicas) both write through it, and nothing
    upstream rules that out. A busy thread fails FAST with `ErrorCode.THREAD_BUSY` rather than queueing behind
    the in-flight job, which would let a double-submit produce two real turns.

    The handler calls `release_lock` (passed in below) BEFORE publishing a terminal event. Both weaker
    orderings were measured against a real 40-way concurrent HITL pause/resume load
    (tests/integration/test_worker_scaling.py):
    - releasing in this function's own `finally` (after the handler fully returns) spuriously rejected ~1/3 of
      resumes as THREAD_BUSY;
    - releasing right after publishing the terminal event, still awaiting `publish_result` first, cut that to
      ~1/8 but didn't close it: `publish_result` awaits a SECOND Redis round trip (a TTL refresh) after its
      `xadd`, and an `xread` reader can see the xadd'd event during it.

    Only releasing BEFORE `publish_result` closes the window: by the time any client can observe the terminal
    event, the lock is already free.
    """
    payload = json.loads(fields["payload"])
    request_id = payload["request_id"]
    kind = payload.get("kind", "turn")
    handler = _DISPATCH.get(kind)
    lock_token = uuid.uuid4().hex
    lock_released = False

    async def release_lock() -> None:
        """Idempotent — safe to call from the handler (the normal path,
        right after a terminal event) AND from this function's own
        `finally` below (the safety net for a handler that raises before
        ever reaching a terminal event, or a legacy/future handler that
        forgets to call it itself). The `nonlocal` guard avoids a wasted
        second Redis round trip on the common path; `release_thread_lock`
        itself is also safe to call twice regardless (compare-and-delete
        against `lock_token`), so this is belt-and-suspenders, not load-bearing."""
        nonlocal lock_released
        if lock_released:
            return
        lock_released = True
        await release_thread_lock(client, thread_id, lock_token)

    with bind_request_id(request_id):
        try:
            thread_id = payload["thread_id"]
            if handler is None:
                raise ValueError(f"unknown job kind: {kind!r}")
            if not await acquire_thread_lock(client, thread_id, lock_token):
                envelope = ErrorEnvelope(
                    code=ErrorCode.THREAD_BUSY,
                    message="Another turn is already in progress on this conversation. Please wait for it to finish.",
                )
                await publish_result(
                    client,
                    request_id,
                    {"type": "error", "content": envelope.message, **envelope.to_dict()},
                )
                return
            try:
                await handler(client, request_id, payload, release_lock)
            finally:
                await release_lock()
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            logger.warning(
                "agent_worker_turn_failed",
                extra={
                    "request_id": request_id,
                    "kind": kind,
                    "error_class": type(exc).__name__,
                    "origin": error_origin(exc),  # where, not what: see errors.error_origin
                },
            )
            # Not `str(exc)`: the caller gets the generic envelope, never the exception's own
            # text (an internal hostname, a SQL fragment, a DSN — pattern 30). The worker's own
            # diagnostics, like the unknown-kind ValueError above, are for the operator's logs.
            envelope = internal_error_envelope(exc)
            await publish_result(
                client, request_id, {"type": "error", "content": envelope.message, **envelope.to_dict()}
            )
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


def _turn_already_completed(turn_messages: list) -> bool:
    """True if the last message in this turn's own slice is a real final
    `AIMessage` (content produced, no pending `tool_calls`) — i.e. the
    graph already reached `check_output`/`END` (or a safety-net fallback)
    for this turn, which means `runtime/stream.py::_record_turn_metrics`
    already ran and recorded this turn's usage/cost. Retrying it — fresh
    OR continued — would re-run/re-tally an answer that already exists,
    silently double-recording that cost for no benefit; a zero-tool-call
    turn that crashed in the thin window between finishing and this job's
    own ack would otherwise look identical to one that never got that far
    at all."""
    if not turn_messages:
        return False
    last = turn_messages[-1]
    # A rejected answer (tagged by `retry_output`, see messages.RETRY_ARTIFACT_KEY) has no
    # tool calls either, but it is the answer the turn is about to be asked to replace.
    return isinstance(last, AIMessage) and not last.tool_calls and not is_retry_artifact(last)


async def _classify_reclaimed_turn(graph, thread_id: str) -> str:
    """Returns `"dead_letter"`, `"retry_fresh"` or `"retry_continue"` for a reclaimed `"turn"`/`"turn_continue"`
    job, by inspecting the thread's checkpointed state. Never re-runs anything (the same read-only
    `graph.aget_state` that `app/agent/runtime/stream.py`'s `get_session_messages`/`resumability_error_async`
    use).

    It finds the last message the PERSON sent (`is_user_message`, not the retry feedback `retry_output` writes
    as a tagged `HumanMessage`). Every turn appends exactly one fresh `HumanMessage` before anything else
    runs, so everything after it belongs to the most recent turn only: the crashed one, or (if the crash came
    before that message was checkpointed) a DIFFERENT, already-completed turn.

    - `"retry_fresh"`: no `HumanMessage` anywhere in the checkpoint, so the thread never reached its first
      turn and there is nothing to continue. Republished as an ordinary `"turn"` (`astream_events_turn`,
      original text).
    - `"dead_letter"`: any of
      - the checkpoint can't be read;
      - the messages after the last `HumanMessage` show the turn already produced its final answer
        (`_turn_already_completed`);
      - the checkpoint is paused at a real `human_approval` interrupt (`state.tasks[i].interrupts`, not just
        `state.next`; see `graph/hitl.py::_resumability_error_from_state` for why `state.next` alone doesn't
        mean "paused"). A `"turn"` job reaches this function only if reclaimed, i.e. its worker died before
        the ack, and a healthy worker acks a job that reaches a pause like one that finishes, so a pause here
        means the crash landed just AFTER the interrupt fired. That belongs to `astream_events_resume`'s
        `Command(resume=...)`, not a bare continuation with no resume value.
    - `"retry_continue"`: anything else; the turn's own `HumanMessage` IS checkpointed and unfinished.
      Republished as `"turn_continue"` (`astream_events_continue_turn`). Safe EVEN IF a mutating/outward tool
      already completed, with no per-tool capability check: continuing the same checkpointed run, rather than
      restarting with `astream_events_turn` (which mints `tool_call_id`s `app/core/tool_idempotency.py` has
      never seen), makes that structurally safe rather than heuristic. See that function's docstring.

    Deliberately conservative in one edge case: if the crash came so early that this turn's own `HumanMessage`
    was never checkpointed, the last one found belongs to the PRECEDING turn, which under normal operation is
    always `_turn_already_completed` (nothing starts a new turn on a thread whose previous one hasn't
    finished). So this reports `"dead_letter"` even though the crashed turn never started. That is a false
    negative (dead-lettering something that was fine to retry fresh), never a false positive, an acceptable
    cost for not tracking where this turn's own messages start.
    """
    try:
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001 - can't prove safety, so treat it as unsafe
        logger.warning(
            "agent_worker_reclaim_checkpoint_unreadable",
            extra={"thread_id": thread_id, "error_class": type(exc).__name__},
        )
        return "dead_letter"

    messages = (state.values or {}).get("messages", []) if state else []
    # The turn's own question, not `retry_output`'s feedback (a HumanMessage too, tagged): see
    # messages.is_user_message. Today the two readings agree, because the feedback and the
    # rejected answer are written together; this keeps them agreeing if that ever changes.
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])),
        None,
    )
    if last_human_index is None:
        return "retry_fresh"  # this thread never even reached its first turn
    turn_messages = messages[last_human_index + 1 :]
    if _turn_already_completed(turn_messages):
        return "dead_letter"
    if state.next and any(task.interrupts for task in state.tasks):
        return "dead_letter"  # crashed strictly after reaching a real pause
    if not state.next:
        # Unfinished (no final answer, checked above), yet nothing is pending to run. A turn
        # that stopped mid-flight always has a next node, so this is a checkpoint whose pending
        # node this build no longer recognises: LangGraph names the channel that triggers a node
        # after the edge feeding it, so a renamed router (or a removed node) hides the pending
        # one and a resume silently runs nothing (verified: tests/agent/graph/test_checkpoint_across_a_topology_change.py).
        # "Continuing" it would end the turn with no answer, so it is dead-lettered instead and
        # the caller is told to retry.
        metrics.agent_checkpoint_issue_total.labels(reason="nothing_to_continue").inc()
        logger.warning("agent_worker_reclaim_nothing_to_continue", extra={"thread_id": thread_id})
        return "dead_letter"
    return "retry_continue"


async def _handle_reclaimed_job(client, entry_id: str, fields: dict, *, graph) -> None:
    """One entry `_reclaim_loop` found idle past
    `AGENT_WORKER_RECLAIM_IDLE_SECONDS` — its original consumer almost
    certainly died before ever reaching `process_request`'s own ack (a live
    worker acks well within that margin; see the config field's own
    docstring for the sizing). See this module's own docstring for the
    per-kind retry-safety policy this function and `_classify_reclaimed_turn`
    implement together; either path finishes by acking this entry so it's
    never reclaimed again."""
    try:
        payload = json.loads(fields["payload"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "agent_worker_reclaimed_payload_unreadable",
            extra={"entry_id": entry_id, "error_class": type(exc).__name__},
        )
        await _dead_letter_reclaimed(client, entry_id, {}, reason="unreadable_payload")
        return

    request_id = payload.get("request_id")
    thread_id = payload.get("thread_id")
    kind = payload.get("kind", "turn")
    attempts = payload.get(RECLAIM_ATTEMPTS_FIELD, 0)
    logger.warning(
        "agent_worker_job_reclaimed",
        extra={
            "entry_id": entry_id,
            "request_id": request_id,
            "thread_id": thread_id,
            "kind": kind,
            "attempts": attempts,
        },
    )

    if kind in ("cancel", "resume"):
        safe_to_retry = True
    elif kind in ("turn", "turn_continue") and thread_id is not None:
        action = await _classify_reclaimed_turn(graph, thread_id)
        safe_to_retry = action != "dead_letter"
        if safe_to_retry:
            # Stamp the DECISION onto the kind that gets republished, not
            # just whether to retry at all — a plain "turn" retry (a fresh
            # astream_events_turn call) is what used to make retrying a
            # turn that already ran a mutating/outward tool unsafe; see
            # _classify_reclaimed_turn's own docstring.
            payload["kind"] = "turn" if action == "retry_fresh" else "turn_continue"
    else:
        safe_to_retry = False

    if safe_to_retry and attempts < MAX_AUTO_RECLAIM_RETRIES:
        await republish_job(client, requests_stream=REQUESTS_STREAM, payload=payload)
        metrics.agent_worker_job_reclaimed_total.labels(queue="agent", outcome="retried").inc()
        await client.xack(REQUESTS_STREAM, CONSUMER_GROUP, entry_id)
        return

    # Not safe (a "turn"/"turn_continue" that already finished or is
    # paused at a real interrupt), or safe but out of retries — surface +
    # archive, same as this always did before retry support existed.
    if request_id:
        envelope = ErrorEnvelope(
            code=ErrorCode.WORKER_LOST,
            message="The worker handling this request stopped responding before it finished. Please try again.",
        )
        await publish_result(
            client, request_id, {"type": "error", "content": envelope.message, **envelope.to_dict()}
        )
    # Deliberately NOT releasing thread_id's lock here: no token to
    # compare-and-delete against (the dead worker held it, not us), and
    # AGENT_WORKER_RECLAIM_IDLE_SECONDS' default is chosen to already
    # exceed THREAD_LOCK_TTL_SECONDS, so by the time a job is reclaimed
    # its lock has already self-expired via that TTL — see both settings'
    # own docstrings in app/core/config.py.
    await _dead_letter_reclaimed(client, entry_id, payload, reason="worker_lost")


async def _dead_letter_reclaimed(client, entry_id: str, payload: dict, *, reason: str) -> None:
    metrics.agent_worker_job_reclaimed_total.labels(queue="agent", outcome="dead_lettered").inc()
    await publish_dead_letter(
        client, requests_stream=REQUESTS_STREAM, entry_id=entry_id, payload=payload, reason=reason
    )
    await client.xack(REQUESTS_STREAM, CONSUMER_GROUP, entry_id)


async def _reclaim_loop(client, stop_event: asyncio.Event, *, graph) -> None:
    """Runs for this whole process's life, alongside `run()`'s own read
    loop: every `WORKER_RECLAIM_INTERVAL_SECONDS`, sweeps this domain's
    requests stream for entries abandoned by a worker that died mid-job
    (any replica's reclaim loop can claim any OTHER replica's abandoned
    entry — `XAUTOCLAIM` operates on the whole consumer group, not just
    this process's own pending list). A transient Redis error is logged and
    retried next interval rather than raised — the one thing worse than a
    slow reclaim pass is no reclaim pass ever again for this process."""
    min_idle_ms = AGENT_WORKER_RECLAIM_IDLE_SECONDS * 1000
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WORKER_RECLAIM_INTERVAL_SECONDS)
            return  # stop_event was set during the wait — shutting down
        except TimeoutError:
            pass  # normal case: interval elapsed, run a pass below
        try:
            entries = await reclaim_stale_entries(
                client,
                stream=REQUESTS_STREAM,
                group=CONSUMER_GROUP,
                consumer=CONSUMER_NAME,
                min_idle_ms=min_idle_ms,
            )
            for entry_id, fields in entries:
                await _handle_reclaimed_job(client, entry_id, fields, graph=graph)
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end reclaim for this process's whole life
            logger.warning("agent_worker_reclaim_pass_failed", extra={"error_class": type(exc).__name__})


async def run() -> None:
    manifest, domain = resolve_domain(AGENT_DOMAIN)  # fails loud on a typo'd AGENT_DOMAIN
    graph = await init_graph_async(manifest=manifest, domain=domain)  # opens the durable
    # checkpointer on THIS process's own loop, against AGENT_DOMAIN's manifest/tools.
    # Kept (not discarded like before reclaim support existed): _reclaim_loop
    # needs it read-only, to check a reclaimed "turn"/"turn_continue" job's
    # checkpointed state via _classify_reclaimed_turn — a process serves
    # exactly one domain for its life, so init_graph_async() would return
    # this exact same cached graph again regardless.
    client = get_client()
    await ensure_consumer_group(client, AGENT_DOMAIN)
    logger.info(
        "agent_worker_started",
        extra={"consumer": CONSUMER_NAME, "domain": manifest.name, "max_concurrency": _MAX_CONCURRENCY},
    )

    # Graceful shutdown: SIGTERM/SIGINT sets this instead of killing the
    # loop mid-read. Checked only BETWEEN xreadgroup calls, never inside
    # the entries loop, so every already-claimed job runs to completion and
    # gets acked before exit. A worker that dies WITHOUT this graceful path
    # (killed, crashed) still leaves an abandoned entry behind, same as
    # ever — that's what _reclaim_loop below, run by every replica in the
    # pool, recovers from.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    reclaim_task = asyncio.create_task(_reclaim_loop(client, stop_event, graph=graph))

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
    await reclaim_task  # stop_event already set above, so this returns promptly
    await client.aclose()
    # Same reasoning as app/api/main.py's lifespan shutdown: the MCP bridge a sandbox tool started is a child process of this one.
    await close_all_connections()
    # Same reasoning as app/api/main.py's lifespan shutdown: a turn may have
    # opened core/sql_store.py's connection pool; leaving it open past exit
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
