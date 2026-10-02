# Contract: Ingest Job Protocol (worker behavior)

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §3, §6–§7](../data-model.md) | **Shared mechanics**: feature 003
[queue-job-protocol.md](../../003-approval-and-exactly-once-writes/contracts/queue-job-protocol.md), feature 004
(`specs/004-scalable-serving-and-front-doors/contracts/worker-process.md`)

**Status**: Retrospective — `app/ingestion/ingest_worker.py`, `ingest_queue.py`; tests in `tests/ingestion/test_ingest_worker.py` and `test_ingest_queue.py`.
**B9 and B10 are open defects**, marked where they break this contract.

## Roles

Producer: the API (`publish_ingest_request`). Consumer: `python -m app.ingestion.ingest_worker` (`make ingest-worker`), N replicas as one consumer group
(`ingest-workers`) on `ingest:requests`. Startup, concurrency (semaphore before the task; thread pool sized to the bound), graceful stop and signal handling are
feature 004's worker contract; there is no domain (one global ingest stream).

## Per job (`process_job`)

| Step | Behavior | On failure |
|------|----------|------------|
| 1 | publish `started` | — |
| 2 | pick the extractor by the file's suffix | unsupported → `ExtractionFailed("unsupported file type '<sfx>' — only [...] are supported")` |
| 3 | `download_bytes` on a worker thread | raises → job fails (B10) |
| 4 | the extractor on a worker thread | `ExtractionFailed` (encrypted, corrupt) → a specific message |
| 5 | `ingest_text(text, title=<stem>, ctx, source="upload:<name>", topic, on_progress)` | `IngestRefused` (e.g. invalid ctx) → its message |
| 6 | publish `done {chunks}` | — |

**Failure policy**: any exception is caught, logged with the exception class and the first 300 characters of its message, published as
`error {content: str(exc)}` (**B10 — no code, unexpected text reaches the uploader**) and the entry is **always acknowledged** (a deterministic failure is not redelivered).
A zero-chunk result is published as `done {chunks: 0}` (**B9**).

## Idempotence

`ingest_text` derives every record id from `(tenant, source, position, text hash)`, so re-running a job — a redelivery, a reclaim, a double upload — upserts onto the same
ids. It does **not** remove records of an earlier version of the same source (**B11**).

## Crash recovery

Every `WORKER_RECLAIM_INTERVAL_SECONDS` (60 s) a loop `XAUTOCLAIM`s entries idle longer than `INGEST_WORKER_RECLAIM_IDLE_SECONDS` (900 s — parsing can take minutes). Per entry:

| Case | Outcome |
|------|---------|
| payload unreadable | dead-letter (`unreadable_payload`), ack |
| `attempts < MAX_AUTO_RECLAIM_RETRIES` (1) | republish with `_reclaim_attempts + 1`, count `retried`, ack — **always safe** |
| out of retries | publish to the job's stream `error "The worker processing this upload kept failing after multiple attempts. Please upload it again."`, dead-letter (`worker_lost`) to `ingest:requests:dead`, count `dead_lettered`, ack |

A transient error in a reclaim pass is logged and retried next interval.

## Progress

`on_progress(done, total)` is awaited after each dense batch and publishes `progress`; a failed publish is logged and dropped (the terminal event is what matters).

## Invariants a change must preserve

1. Ack after the handler ends, success or failure.
2. A re-run of a job converges on the same records.
3. Blocking work never runs on the event loop.
4. A job's terminal event is published exactly once per attempt.
5. The uploader never receives text the codebase does not control about how the system is wired (**B10**) or a success for work that stored nothing (**B9**).
