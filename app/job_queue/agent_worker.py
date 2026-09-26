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
(`asyncio.Semaphore`-bounded `create_task` per job). Safe across DIFFERENT
thread_ids because a turn holds no in-process state a sibling could
corrupt: conversation state lives in Postgres via the checkpointer,
tenant-scoped resources are per-call/pooled, and `bind_request_id`'s
`ContextVar` is copied per-task by `asyncio.create_task`, not shared.
`_MAX_CONCURRENCY` lets the slow parts (LLM calls, tool execution) of
several turns overlap — it doesn't eliminate the checkpointer's own
finer-grained serialization point (see `app/agent/runtime.py::_open_checkpointer`).

For the SAME thread_id, two jobs are NOT independent even though neither
holds shared in-process state: both would read the checkpointer's latest
state as their own parent and both write a child from it, so whichever
commits last silently wins — `process_request` acquires
`queue.py::acquire_thread_lock` before running any job specifically to
rule this out (across this process's own concurrent tasks AND across
other worker replicas sharing the same Redis), rejecting a losing job
with `ErrorCode.THREAD_BUSY` rather than letting it run.

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
then ack so it's never redelivered. A worker that dies mid-job (crash, OOM
kill, host failure) before reaching that ack leaves its request pending for
the group forever — `_reclaim_loop` (run alongside the main read loop by
every worker in the pool) periodically sweeps for exactly that via
`queue.py::reclaim_stale_entries` (XAUTOCLAIM) and hands each one to
`_handle_reclaimed_job`, which decides PER JOB whether re-running it is
provably safe:
- `"cancel"` — always safe (cancelling is inherently idempotent: it either
  still applies, or there's nothing left to cancel and it's a no-op either
  way).
- `"resume"` — also always safe, since `app/agent/tool_idempotency.py`
  started guarding every `mutating`/`outward` tool: a resume continues an
  EXISTING checkpoint from its `human_approval` pause, re-invoking whichever
  tool calls were already pending under the exact SAME `tool_call_id`s (the
  pause happens before those calls run) — any that already completed just
  return their cached result instead of running again. Before that
  guarantee existed, "resume" was deliberately excluded here (no fresh
  `HumanMessage` boundary the way "turn" has, so there was no cheap way to
  tell "did the just-approved call already run") — tool-level idempotency
  is what closed that gap, not anything added to this function itself.
- `"turn"` — safe only if `_is_safe_to_retry_turn` finds BOTH no completed
  `mutating`/`outward` tool call (per this domain's own
  `DomainPlugin.tool_capabilities()`) AND no already-produced final answer
  (`_turn_already_completed`) in the thread's checkpointed state since its
  last `HumanMessage` — i.e. the crash happened before this turn did
  anything irreversible OR finished at all (most commonly: during LLM
  inference, the single slowest and most frequent step). The
  already-completed check exists because a turn can finish (and have its
  usage/cost already recorded, `runtime_stream.py::_record_turn_metrics`)
  with ZERO tool calls — the tool-call check alone would call that "safe"
  and blindly re-run a turn that had nothing left to do, double-recording
  its cost. Tool-level idempotency does NOT extend to either case: a
  retried "turn" re-asks the LLM from scratch, which gets brand-new
  tool_call_ids unrelated to whatever the crashed attempt's own tool_calls
  were, so dedup can never "catch" a duplicate there.
- Every safe case above is silently republished via `queue.py::republish_job`,
  up to `MAX_AUTO_RECLAIM_RETRIES` attempts (`queue.py::RECLAIM_ATTEMPTS_FIELD`
  on the payload) — beyond that, or once a "turn" is found to have already
  run a mutating/outward call, it's surfaced as a `WORKER_LOST` error on the
  job's own results stream and archived to a dead-letter stream
  (`queue.py::dead_letter_stream_key`) for inspection/manual replay.

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

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import sql_store
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.agent.runtime_stream import (
    astream_events_resume,
    astream_events_turn,
    cancel_run,
)
from app.core import metrics
from app.core.config import (
    AGENT_DOMAIN,
    AGENT_WORKER_MAX_CONCURRENCY,
    AGENT_WORKER_RECLAIM_IDLE_SECONDS,
    MAX_AUTO_RECLAIM_RETRIES,
    WORKER_RECLAIM_INTERVAL_SECONDS,
)
from app.core.errors import ErrorCode, ErrorEnvelope
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


async def _process_turn(client, request_id: str, payload: dict, release_lock) -> None:
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


async def _process_resume(client, request_id: str, payload: dict, release_lock) -> None:
    async for event in astream_events_resume(
        payload["thread_id"], payload["approved"], payload["ctx"]
    ):
        if event.get("type") in TERMINAL_EVENT_TYPES:
            await release_lock()  # see _process_turn's own comment on this
        await publish_result(client, request_id, event)


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
    await release_lock()  # single-shot job, always terminal — see _process_turn's own comment
    await publish_result(client, request_id, event)


_DISPATCH = {"turn": _process_turn, "resume": _process_resume, "cancel": _process_cancel}


async def process_request(client, entry_id: str, fields: dict) -> None:
    """Dispatch one job by `payload["kind"]` and stream its events back —
    always ack, even on failure: a redelivered, already-attempted request
    would re-run the same side-effecting tool calls twice.

    Every job kind first acquires `queue.py::acquire_thread_lock` for its
    `thread_id` — this module's own docstring notes a turn holds no
    IN-PROCESS state a sibling could corrupt, which is true of THIS
    process's memory but not of the shared Postgres checkpointer two
    DIFFERENT jobs on the SAME thread_id both write through (a
    double-submit, a retry, or a resume racing a fresh turn — nothing
    upstream of this dispatcher rules that out, and it can just as easily
    land on two different worker replicas as on two tasks here). A thread
    already busy fails FAST with `ErrorCode.THREAD_BUSY` instead of
    silently queueing behind the in-flight job, which would let a
    double-submit produce two real turns instead of one.

    The handler itself calls `release_lock` (passed in below) BEFORE
    publishing a terminal event, not after, and not this function waiting
    for the handler to fully RETURN. Both weaker orderings were tried and
    measured against a real 40-way concurrent HITL pause/resume load
    (tests/integration/test_worker_scaling.py) before landing on this one:
    releasing in this function's own `finally` (waiting for the handler to
    fully return) left ~1/3 of resumes spuriously rejected as THREAD_BUSY;
    releasing right after publishing the terminal event (but still
    awaiting `publish_result` first) cut that to ~1/8 but didn't close it —
    `publish_result` itself awaits a SECOND Redis round trip (a TTL
    refresh) after its `xadd`, and a reader polling via `xread` can already
    see the xadd'd event during that second round trip. Only releasing
    BEFORE calling `publish_result` at all closes the window completely:
    by the time any client could possibly observe the terminal event, the
    lock is unconditionally already free."""
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


def _turn_already_completed(turn_messages: list) -> bool:
    """True if the last message in this turn's own slice is a real final
    `AIMessage` (content produced, no pending `tool_calls`) — i.e. the
    graph already reached `check_output`/`END` (or a safety-net fallback)
    for this turn, which means `runtime_stream.py::_record_turn_metrics`
    already ran and recorded this turn's usage/cost. Retrying from scratch
    would re-run the whole LLM call again for an answer that already
    exists, silently double-recording that cost for no benefit — a real,
    if narrow, gap: `_is_safe_to_retry_turn`'s own tool-call check alone
    says nothing about whether the turn had already finished, so a
    zero-tool-call turn that crashed in the thin window between finishing
    and this job's own ack used to be judged safe and blindly re-run."""
    if not turn_messages:
        return False
    last = turn_messages[-1]
    return isinstance(last, AIMessage) and not last.tool_calls


async def _is_safe_to_retry_turn(graph, tool_capabilities: dict[str, str], thread_id: str) -> bool:
    """True only if this thread's checkpointed state proves nothing
    irreversible AND nothing already-finished happened for its most recent
    turn — i.e. re-running that turn from scratch (a fresh
    `astream_events_turn` call) cannot duplicate a real side effect or
    double-record a completed turn's usage cost.

    Reads the checkpoint via `graph.aget_state` (never re-runs anything —
    same read-only call `app/agent/runtime_stream.py::get_session_messages`/
    `resumability_error_async` already use elsewhere) and finds the LAST
    `HumanMessage` in it: since every turn appends exactly one fresh
    `HumanMessage` before anything else runs, everything after that message
    belongs to the most recent turn only — whether that's the crashed one,
    or (if the crash happened before even that got checkpointed) a
    DIFFERENT, already-completed turn. Two things make it unsafe:
    - `_turn_already_completed` — the turn already produced its final
      answer (see that function's own docstring for the double-billing gap
      this closes).
    - A `ToolMessage` after the last `HumanMessage` whose tool is
      `mutating`/`outward` (capability missing from the mapping defaults to
      `outward` — fail closed, same default `graph_routing.py` itself
      uses) — something irreversible already ran and must not run again.

    Deliberately conservative in one specific edge case: if the crash
    happened SO early no checkpoint for the new turn was ever written, this
    falls back to inspecting the PRECEDING (unrelated, already-completed)
    turn instead — which is, by definition, always `_turn_already_completed`
    (it did finish) and may itself contain a mutating/outward call — so it
    reports "not safe" even though the crashed turn genuinely never
    started. That's a false negative (dead-letters something that was
    actually fine to retry), never a false positive, so it's an acceptable
    cost for not needing separate bookkeeping of "where did this turn's own
    messages start."

    Fails closed (returns False) if the checkpoint itself can't be read —
    no existing caller in this codebase handles a corrupt (not just
    missing) checkpoint either; treating "can't tell" as "not safe" is the
    same default `graph_routing.py`'s own capability lookup uses.
    """
    try:
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001 - can't prove safety, so it isn't
        logger.warning(
            "agent_worker_reclaim_checkpoint_unreadable",
            extra={"thread_id": thread_id, "error_class": type(exc).__name__},
        )
        return False

    messages = (state.values or {}).get("messages", []) if state else []
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    if last_human_index is None:
        return True  # this thread never even reached its first turn
    turn_messages = messages[last_human_index + 1 :]
    if _turn_already_completed(turn_messages):
        return False
    for message in turn_messages:
        # `message.name or ""` handles the (untyped-as-impossible-but-not
        # enforced) case of a nameless ToolMessage the same as an unknown
        # tool: no match in `tool_capabilities` -> the "outward" fail-closed
        # default below.
        if (
            isinstance(message, ToolMessage)
            and tool_capabilities.get(message.name or "", "outward") != "read_only"
        ):
            return False
    return True


async def _handle_reclaimed_job(
    client, entry_id: str, fields: dict, *, graph, tool_capabilities: dict[str, str]
) -> None:
    """One entry `_reclaim_loop` found idle past
    `AGENT_WORKER_RECLAIM_IDLE_SECONDS` — its original consumer almost
    certainly died before ever reaching `process_request`'s own ack (a live
    worker acks well within that margin; see the config field's own
    docstring for the sizing). See this module's own docstring for the
    per-kind retry-safety policy this function and `_is_safe_to_retry_turn`
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

    safe_to_retry = kind in ("cancel", "resume") or (
        kind == "turn"
        and thread_id is not None
        and await _is_safe_to_retry_turn(graph, tool_capabilities, thread_id)
    )
    if safe_to_retry and attempts < MAX_AUTO_RECLAIM_RETRIES:
        await republish_job(client, requests_stream=REQUESTS_STREAM, payload=payload)
        metrics.agent_worker_job_reclaimed_total.labels(queue="agent", outcome="retried").inc()
        await client.xack(REQUESTS_STREAM, CONSUMER_GROUP, entry_id)
        return

    # Not safe (a "resume", or a "turn" that already ran a mutating/outward
    # tool call), or safe but out of retries — surface + archive, same as
    # this always did before retry support existed.
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


async def _reclaim_loop(client, stop_event: asyncio.Event, *, graph, tool_capabilities: dict[str, str]) -> None:
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
                await _handle_reclaimed_job(
                    client, entry_id, fields, graph=graph, tool_capabilities=tool_capabilities
                )
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end reclaim for this process's whole life
            logger.warning("agent_worker_reclaim_pass_failed", extra={"error_class": type(exc).__name__})


async def run() -> None:
    manifest, domain = resolve_domain(AGENT_DOMAIN)  # fails loud on a typo'd AGENT_DOMAIN
    graph = await init_graph_async(manifest=manifest, domain=domain)  # opens the durable
    # checkpointer on THIS process's own loop, against AGENT_DOMAIN's manifest/tools.
    # Kept (not discarded like before reclaim support existed): _reclaim_loop
    # needs it read-only, to check a reclaimed "turn" job's checkpointed
    # state via _is_safe_to_retry_turn — a process serves exactly one
    # domain for its life, so init_graph_async() would return this exact
    # same cached graph again regardless.
    tool_capabilities = domain.tool_capabilities()  # same reason: computed
    # once, since it can't change for this process's whole life either.
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

    reclaim_task = asyncio.create_task(
        _reclaim_loop(client, stop_event, graph=graph, tool_capabilities=tool_capabilities)
    )

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
