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

A second per-THREAD mechanism: the thread lock (`thread_lock_key`/
`acquire_thread_lock`/`release_thread_lock`) — mutual exclusion across
every job kind touching one `thread_id`'s checkpoint. The consumer-group
split above guarantees no ONE request is delivered twice, but says
nothing about two DIFFERENT requests (a double-submit, a client retry, a
resume racing a new turn) landing on the SAME thread_id at the same real
moment, possibly on two different worker replicas. Two such jobs both
read the checkpointer's "latest" state as their parent and both write a
child from it — whichever commits last silently wins, and the other
job's turn vanishes from the thread's visible history. `agent_worker.py`
acquires this lock before running ANY job for a thread_id and rejects a
losing job fast (`ErrorCode.THREAD_BUSY`) rather than queueing it behind
the winner — but only while both are genuinely CONCURRENT: a retry that
arrives after the first job already finished races nothing, the lock is
free again, and (without the third mechanism below) would run as a
second, fully independent turn.

A third per-THREAD mechanism: submission dedup
(`claim_or_get_existing_submission`) — closes exactly that gap, for
`POST /chat/stream/queued` specifically. See its own docstring.

This module only wraps queue mechanics (publish/read, group setup) — it
doesn't know what a "turn" or "event" IS. Producer: `app/api/main.py`
(`POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel`). Consumer:
`app/job_queue/agent_worker.py`, the only place that runs the graph.
"""
import json
import logging
from typing import cast

import redis.asyncio as redis

from app.core.config import REDIS_MAX_CONNECTIONS, REDIS_URL, REQUEST_TIMEOUT_SECONDS
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
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "approval_required"})  # the
# last event type `runtime_stream.py::_run_graph_stream` ever yields for a
# given job — shared by `read_results` (stop reading) and
# `agent_worker.py`'s handlers (release `acquire_thread_lock` the INSTANT
# one of these is published, not after their own generator's later
# cleanup work finishes — see agent_worker.py::process_request's
# `release_lock` docstring for the race this closes).
RESULTS_STREAM_TTL_SECONDS = 300  # an abandoned results stream self-expires after 5 minutes
CANCEL_FLAG_TTL_SECONDS = 60  # outlasts any gap between a worker's cancel-check
# polls; short enough that a flag nobody consumed (thread finished before
# /chat/cancel ran) doesn't linger and spuriously cancel a later turn.
THREAD_LOCK_TTL_SECONDS = REQUEST_TIMEOUT_SECONDS * 2  # a safety net, not the
# normal release path (agent_worker.py always releases explicitly in a
# `finally`) — outlives the longest legitimate "turn" job (bounded by
# REQUEST_TIMEOUT_SECONDS's own _iterate_with_timeout) plus a wide margin
# for a "resume" job, which isn't wrapped in that same timeout. Bounds how
# long a worker that dies mid-turn can wedge its thread_id: the lock
# self-expires and a later job can proceed, instead of every future job on
# that thread getting THREAD_BUSY forever.

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


def thread_lock_key(thread_id: str) -> str:
    return f"agent:lock:{thread_id}"


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


# --- A THIRD per-thread mechanism: submission dedup — closes a gap the
# thread lock above does NOT: that lock only rules out two jobs for the
# SAME thread_id running CONCURRENTLY. A double-submit (a client's own
# network-level retry, or a double-click) that arrives AFTER the first
# attempt already finished races nothing — the lock is free again, so the
# retry runs as a genuinely new, independent turn: a second LLM call, with
# its own fresh tool_call_ids, that can execute a real mutating tool call a
# SECOND time (app/agent/tool_idempotency.py can't catch this either — its
# dedup is keyed by tool_call_id, and an independently-decided second turn
# never reuses the first's). `claim_or_get_existing_submission` closes that
# specific window: identical (thread_id, text, images) submitted twice
# within `ttl_seconds` reuses the FIRST attempt's own request_id instead of
# publishing a second `"turn"` job, so the retrying caller's SSE connection
# transparently gets that same turn's real events instead of either a
# duplicate execution or a bare THREAD_BUSY error.
def _submission_dedup_key(thread_id: str, digest: str) -> str:
    return f"chat:submit_dedup:{thread_id}:{digest}"


async def claim_or_get_existing_submission(
    client: redis.Redis, *, thread_id: str, digest: str, request_id: str, ttl_seconds: int
) -> tuple[str, bool]:
    """`digest` is the caller's own stable hash of whatever makes two
    submissions "the same" (text + images, for `POST /chat/stream/queued`)
    — this function doesn't compute it, just claims a slot for it.　Returns
    `(request_id_to_use, is_new_submission)`: the atomic `SET NX EX` either
    lands (this is the first submission this window has seen — proceed to
    publish a real job under `request_id`) or it doesn't (an identical
    submission already claimed this window — reuse ITS request_id, and the
    caller must NOT publish a second job).

    Narrow accepted race: if the key expires between this call's failed
    `SET` and its follow-up `GET` (a window of at most a few ms), this
    falls back to claiming it fresh under the caller's OWN `request_id`
    rather than looping — functionally identical to that key never having
    existed at all, just resolved on this call instead of forcing a retry.
    """
    key = _submission_dedup_key(thread_id, digest)
    claimed = bool(await client.set(key, request_id, nx=True, ex=ttl_seconds))
    if claimed:
        return request_id, True
    existing = await client.get(key)
    if existing is None:
        await client.set(key, request_id, ex=ttl_seconds)
        return request_id, True
    # decode_responses=True (get_client's own contract) means this is
    # always str at runtime; see StreamReadResponse's own comment on the
    # same redis-py stub-typing gap.
    return cast(str, existing), False


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
            if event.get("type") in TERMINAL_EVENT_TYPES:
                return


async def delete_results_stream(client: redis.Redis, request_id: str) -> None:
    """Best-effort cleanup once the producer has consumed a terminal event —
    not required for correctness (TTL already bounds a leaked stream's
    lifetime), just avoids waiting out the full TTL on the common path."""
    try:
        await client.delete(results_stream_key(request_id))
    except Exception as exc:  # noqa: BLE001 - cleanup is optional, never worth failing a turn over
        logger.warning("queue_results_cleanup_failed", extra={"error_class": type(exc).__name__})


# --- Crash recovery for a requests stream's consumer group (agent_worker.py
# and ingest_worker.py both use these against their own stream/group) ---
#
# Neither worker's own xreadgroup loop redelivers on failure — process_request/
# process_job always ack, even on an exception, specifically so a redelivered
# job never re-runs already-applied side effects (a duplicated tool call, a
# duplicated vector upsert). That leaves exactly one failure mode uncovered:
# the WORKER PROCESS ITSELF dying mid-job (OOM kill, host failure, a bug that
# segfaults the interpreter) before it ever reaches its own try/finally.
# Redis never learns that job failed — the entry just sits in the consumer
# group's Pending Entries List forever, unacked and undelivered to anyone
# else, and whoever's waiting on its results stream hangs until their own
# client-side timeout.
#
# The fix is NOT to blindly redeliver it either, for the same duplicate-
# side-effect reason above — a job reclaimed this way is instead surfaced
# as a failure (an error event on its own results stream) and archived to a
# dead-letter stream for operator inspection/manual replay, then acked so
# it's never attempted again. See agent_worker.py's/ingest_worker.py's own
# reclaim loops for the policy; this module only owns the Streams mechanics.
DEAD_LETTER_MAXLEN = 1000  # approx-trimmed (see publish_dead_letter) — bounds
# growth for an unattended deployment; operators pull recent entries, not
# the full history.


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
# bookkeeping field on the job payload, never set by a real producer
# (publish_request/publish_resume_request/publish_cancel_request never
# write it) — only by republish_job below, and only read by
# agent_worker.py's own retry-cap check.


async def republish_job(client: redis.Redis, *, requests_stream: str, payload: dict) -> str:
    """Re-enqueues `payload` onto `requests_stream` as a brand-new entry —
    the recovery half of a reclaim decided safe to retry (see
    `app/job_queue/agent_worker.py::_handle_reclaimed_job`/
    `_is_safe_to_retry_turn` for that decision). A fresh XADD rather than
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
