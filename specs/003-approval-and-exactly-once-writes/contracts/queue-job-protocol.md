# Contract: Queue Job Protocol and Crash Recovery

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §7–§8](../data-model.md) | **HTTP**: feature 001 [chat-turn-http.md](../../001-core-rag-agent-turn/contracts/chat-turn-http.md)

**Status**: Retrospective — `app/job_queue/queue.py`, `app/job_queue/agent_worker.py`,
`app/api/main.py`. Redis Streams semantics are relied on as documented; the real-Redis behavior is
exercised by `tests/integration/test_queue_real_redis.py` and `test_worker_scaling.py`, **not** by the
hermetic tier (fake Redis).

## Roles

- **Producer** (`app/api/main.py`): publishes a job onto `agent:requests:<domain>`, then reads the job's results
  stream and relays it as SSE.
- **Consumer** (`app/job_queue/agent_worker.py`): one process serves **one domain** for its life
  (`AGENT_DOMAIN`); N replicas share a consumer group; runs up to `AGENT_WORKER_MAX_CONCURRENCY` (10) jobs
  concurrently.

## Delivery

| Property | Guarantee |
|----------|-----------|
| One delivery per request in normal operation | consumer-group semantics |
| **At-least-once** after a worker **crash** | the entry is acked only after the handler finishes, so an unacked entry is reclaimed |
| **At-most-once** after a handler **failure** | `process_request` acks in `finally`; the failure is published as an `error` and the job is **not** redelivered |
| Order within a conversation | not provided — mutual exclusion instead (below) |

## Job kinds

`kind` defaults to `turn`. Domain is inferred from the stream; it is never in the payload. Fields: data-model §7.1.

| `kind` | Handler | Runs | Reclaim-safe? |
|--------|---------|------|---------------|
| `turn` | `astream_events_turn` (attended; wired with a cancel check) | a new turn | via classification |
| `turn_continue` | `astream_events_continue_turn` | the crashed turn's checkpointed run (`astream_events(None, …)`) | via classification |
| `resume` | `astream_events_resume` | approve/reject a paused call (wired with a cancel check and a stale-flag clear since #65 — A12) | **always** (a call that already ran returns its cached result) |
| `cancel` | `cancel_run` | cancels a *paused* conversation | **always** |

## Mutual exclusion: one active job per conversation

`acquire_thread_lock(thread_id, token)` = `SET agent:lock:<thread_id> <token> NX EX <2×turn timeout>`, taken
before **any** job kind runs. A losing job fails fast: `error{code:"thread_busy"}` ("Another turn is already in
progress on this conversation. Please wait for it to finish."). Release is a Lua compare-and-delete against the
holder's token, and the handler releases **before** publishing the terminal event (so a client acting on the
terminal event never finds the lock still held). A crashed holder's lock self-expires.

## Identical resubmission

`POST /chat/stream/queued` claims `chat:submit_dedup:<thread_id>:<sha256(message, images)>` (10 s). A hit reuses
the first attempt's request id and stream and publishes nothing. If publishing fails after a winning claim, the
claim is released.

## Crash recovery

A loop in every worker sweeps its domain stream every `WORKER_RECLAIM_INTERVAL_SECONDS` (60 s) for entries idle
longer than `AGENT_WORKER_RECLAIM_IDLE_SECONDS` (240 s — deliberately above the 120 s lock TTL) using `XAUTOCLAIM`,
so any replica can claim any other replica's abandoned entry. Per entry:

| Kind | Decision | Mechanism |
|------|----------|-----------|
| `cancel`, `resume` | retry | republish |
| `turn`, `turn_continue` | classify from the checkpoint (read-only): see table | |

| Checkpoint observation | Outcome |
|------------------------|---------|
| unreadable | dead-letter |
| no `HumanMessage` ever saved | `retry_fresh` → republish as `turn` |
| the turn already ended in a final answer | dead-letter (retrying would only re-record its cost) |
| paused at a real interrupt | dead-letter (a resume's job, not a bare continuation) |
| anything else (turn started, not finished) | `retry_continue` → republish as `turn_continue` |

**The core invariant**: a reclaimed turn is *continued*, never restarted, because a restart re-asks the model and
mints new call ids no id-keyed defense recognizes; continuing matches already-recorded task writes instead of
re-executing them.

Retries are capped at `MAX_AUTO_RECLAIM_RETRIES` (1; counter in the payload field `_reclaim_attempts`). A job that
is not safe to retry, or is out of retries, yields — on the job's own results stream —
`error{code:"worker_lost", message:"The worker handling this request stopped responding before it finished. Please try again."}`,
is archived to `agent:requests:<domain>:dead` (`{original_entry_id, reason, payload}`, approx. 1000 entries), and is
acked. Metric: `agent_worker_job_reclaimed_total{queue="agent", outcome ∈ retried|dead_lettered}`.

The dead-lettered job's thread lock is **not** released by the reclaimer (no token to compare); it has already
self-expired because the idle threshold exceeds the TTL.

## Results streams and deadlines

`agent:results:<request_id>`: written by the worker, read by the producer; TTL 300 s refreshed on every write;
**not** deleted on a terminal event (two de-duplicated readers may share it). `read_results` bounds the wait for the
**first** event (`CHAT_FIRST_RESPONSE_DEADLINE_SECONDS`, 30 s) and then yields one `error` (no `code`) if nobody
published; a live, slow turn is never cut short by it. Terminal events: `done`, `error`, `approval_required`.

## Client-visible error codes from this layer

`thread_busy` · `worker_lost` · `cancelled` · `pending_approval` (via the runtime) — all enveloped.
Un-enveloped from this layer: the first-event deadline and the worker catch-all (`content: str(exc)`) — feature 001
advisory A2.

## Invariants a change must preserve

1. Ack only after the handler finishes or fails; never ack on receipt.
2. A reclaimed turn that has started is continued, not restarted; a finished or paused one is never retried.
3. At most one active job per `thread_id`; release the lock before publishing the terminal event.
4. Every wait on a counterparty has a deadline.
5. A winning dedup/submission claim whose publish fails is compensated.
6. A handler is safe to run twice for the same logical action (Principle IV) — because delivery is at-least-once.
