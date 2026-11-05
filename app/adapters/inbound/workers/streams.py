"""The agent worker's side of the Redis Streams queue: reading the requests stream as one of a consumer group, and what a worker does to a job around it.

This is the worker's own technology, so it is here and not behind a port: nothing in the application calls it. The names it shares with the producer
(`app/ports/outbound/jobs.py`) are plain data there. Crash recovery's policy (what to retry, what to dead-letter) is `agent_worker.py`'s; this module
owns only the Streams mechanics.
"""
import json
from typing import cast

import redis.asyncio as redis

from app.ports.outbound.jobs import (
    CONSUMER_GROUP,
    RESULTS_STREAM_TTL_SECONDS,
    THREAD_LOCK_TTL_SECONDS,
    cancel_flag_key,
    requests_stream_key,
    results_stream_key,
    thread_lock_key,
)


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


async def is_cancelled(client: redis.Redis, thread_id: str) -> bool:
    return bool(await client.get(cancel_flag_key(thread_id)))


async def clear_cancel_flag(client: redis.Redis, thread_id: str) -> None:
    """Called right before a worker starts a NEW `"turn"` job for a
    thread_id — clears any stale flag left from a PRIOR turn (e.g.
    /chat/cancel raced with that turn finishing on its own) so it can't
    spuriously cancel this new one."""
    await client.delete(cancel_flag_key(thread_id))


# Compare-and-delete, not a bare DEL: a bare delete on the RELEASE side
# could remove a DIFFERENT holder's lock if this one's TTL already expired
# (e.g. a turn that ran longer than THREAD_LOCK_TTL_SECONDS) and a new job
# already acquired it in between — the classic Redis distributed-lock
# release bug. The GET-then-DEL must be one atomic server-side op (Lua),
# not two separate round trips, or that exact race reopens between them.
_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


async def acquire_thread_lock(client: redis.Redis, thread_id: str, token: str) -> bool:
    """Mutual exclusion across every job kind (`"turn"`/`"resume"`/
    `"cancel"`) touching one thread_id's checkpoint, across every worker
    replica sharing this Redis — see this module's own docstring for the
    checkpoint-fork race this closes. `SET NX` is itself atomic (unlike
    a separate GET-then-SET), so ACQUIRE needs no script the way RELEASE
    does. `token` (the caller's own fresh uuid4 per acquire, never reused)
    is what lets `release_thread_lock` tell "I still hold this" apart from
    "this expired and a different job now holds it."""
    return bool(
        await client.set(thread_lock_key(thread_id), token, nx=True, ex=THREAD_LOCK_TTL_SECONDS)
    )


async def release_thread_lock(client: redis.Redis, thread_id: str, token: str) -> None:
    """No-ops (not an error) if `token` doesn't match the current holder —
    that's the expected shape once THREAD_LOCK_TTL_SECONDS has passed and
    a later job already acquired it; this caller no longer holds anything
    worth releasing."""
    await client.eval(_RELEASE_LOCK_SCRIPT, 1, thread_lock_key(thread_id), token)


async def publish_result(client: redis.Redis, request_id: str, event: dict) -> None:
    """Consumer side: append one typed event (same shapes
    `runtime/stream.py::_run_graph_stream` yields — token, tool_start,
    tool_end, citations, error, done) to this request's results stream,
    refreshing its TTL so a long-running turn doesn't expire mid-flight."""
    key = results_stream_key(request_id)
    await client.xadd(key, {"payload": json.dumps(event)})
    await client.expire(key, RESULTS_STREAM_TTL_SECONDS)


# --- Crash recovery for a requests stream's consumer group (agent_worker.py and ingest_worker.py use these
# against their own stream/group) ---
#
# Neither worker's xreadgroup loop redelivers on failure: process_request/process_job always ack, even on an
# exception, so a redelivered job never re-runs applied side effects (a duplicated tool call or vector
# upsert). That leaves one uncovered failure: the WORKER PROCESS dying mid-job (OOM, host failure, a segfault)
# before its own try/finally. Redis never learns the job failed, the entry sits in the consumer group's
# Pending Entries List unacked, and whoever waits on its results stream hangs until their own client timeout.
#
# Blind redelivery is wrong for the same duplicate-side-effect reason. A job reclaimed this way is surfaced as
# a failure (an error event on its results stream), archived to a dead-letter stream for inspection or manual
# replay, and acked so it is never attempted again. The policy is in the workers' own reclaim loops; this
# module owns only the Streams mechanics.
DEAD_LETTER_MAXLEN = 1000  # approx-trimmed (see publish_dead_letter) — bounds


def dead_letter_stream_key(requests_stream: str) -> str:
    return f"{requests_stream}:dead"


async def reclaim_stale_entries(
    client: redis.Redis,
    *,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int,
    count: int = 50,
) -> list[tuple[str, dict[str, str]]]:
    """Drains every entry in `stream`'s `group` that's been claimed but not
    acked for at least `min_idle_ms` — a worker taking that long to still be
    legitimately working one is presumed dead, not slow (callers pick
    `min_idle_ms` well above their own longest legitimate job, see
    app/core/config.py's `*_reclaim_idle_seconds`). Uses `XAUTOCLAIM`, which
    atomically claims matching entries under `consumer` as a side effect —
    the caller must handle (ack or otherwise resolve) every entry this
    returns, or it'll simply become idle under its new owner and get
    reclaimed again next pass.

    Paginates via XAUTOCLAIM's own cursor until it returns `"0-0"` (drained
    for this pass), same loop shape as any Redis SCAN-family cursor.
    """
    claimed: list[tuple[str, dict[str, str]]] = []
    cursor = "0-0"
    while True:
        response = await client.xautoclaim(
            stream, group, consumer, min_idle_ms, start_id=cursor, count=count
        )
        # redis-py returns [next_cursor, [[id, fields], ...], deleted_ids] on
        # modern Redis (the third element — entries claimed then found
        # already trimmed from the stream — is irrelevant here, nothing to
        # ack for an entry that no longer exists); older servers omit it.
        cursor, entries = response[0], response[1]
        claimed.extend((entry_id, fields) for entry_id, fields in entries)
        if cursor == "0-0":
            return claimed


async def publish_dead_letter(
    client: redis.Redis, *, requests_stream: str, entry_id: str, payload: dict, reason: str
) -> None:
    """Archives one reclaimed (presumed-crashed-worker) job for operator
    inspection or manual replay. Callers ack the original entry separately,
    afterward — once that ack happens the entry is gone from its source
    stream's pending-entries list for good, so this archive is the only
    remaining record of it."""
    await client.xadd(
        dead_letter_stream_key(requests_stream),
        {
            "original_entry_id": entry_id,
            "reason": reason,
            "payload": json.dumps(payload),
        },
        maxlen=DEAD_LETTER_MAXLEN,
        approximate=True,
    )


RECLAIM_ATTEMPTS_FIELD = "_reclaim_attempts"  # leading underscore: an internal


async def republish_job(client: redis.Redis, *, requests_stream: str, payload: dict) -> str:
    """Re-enqueues `payload` onto `requests_stream` as a brand-new entry —
    the recovery half of a reclaim decided safe to retry (see
    `app/adapters/inbound/workers/agent_worker.py::_handle_reclaimed_job`/
    `_classify_reclaimed_turn` for that decision). A fresh XADD rather than
    any Streams-native redelivery, deliberately: the retried job then goes
    through the exact same `xreadgroup` → `process_request`/`process_job`
    path as any first attempt, no separate "resumed job" code path to keep
    correct. Reuses the original `request_id`/`job_id` (mutates
    `payload[RECLAIM_ATTEMPTS_FIELD]` in place, everything else untouched)
    so the caller still listening on that request's own results stream
    transparently sees whatever the retry produces — success, or eventually
    another error — without needing to notice a retry happened at all.
    """
    payload[RECLAIM_ATTEMPTS_FIELD] = payload.get(RECLAIM_ATTEMPTS_FIELD, 0) + 1
    # decode_responses=True (get_client's own contract) means this is
    # always str at runtime; redis-py's stubs just type xadd's return
    # wider than that (see StreamReadResponse's own comment on the same
    # gap).
    return cast(str, await client.xadd(requests_stream, {"payload": json.dumps(payload)}))
