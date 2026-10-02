# Contract: Worker Process (startup, concurrency, shutdown)

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §2, §6–§7](../data-model.md) | **Job protocol**: feature 003
[queue-job-protocol.md](../../003-approval-and-exactly-once-writes/contracts/queue-job-protocol.md)

**Status**: Retrospective — `app/job_queue/agent_worker.py::run`, `app/job_queue/queue.py`,
`docker-compose.prod.yml`. Hermetic tests cover `process_request`, the dispatch semaphore and the recovery loop; the
**shutdown path is not tested** (A1).

## Invocation

`python -m app.job_queue.agent_worker` (`make agent-worker`, `-support`, `-ops`, `-sales`; compose services
`agent-worker`, `agent-worker-support`, `-ops`, `-sales`). One domain per process, from `AGENT_DOMAIN`.

## Startup order (each step must succeed before the next)

| # | Step | Failure behavior |
|---|------|------------------|
| 1 | `resolve_domain(AGENT_DOMAIN)` | unknown name → `ValueError` listing the valid domains; process exits |
| 2 | `init_graph_async(manifest, domain)` — opens the checkpointer pool **on this event loop** | store unreachable → process exits |
| 3 | `get_client()` + `ensure_consumer_group(domain)` with `id="0"` (stream created if absent; a group that already exists is fine) | Redis unreachable → fails fast on connect |
| 4 | install SIGTERM and SIGINT → set `stop_event` | — |
| 5 | start the recovery loop (feature 003) | a failed pass is logged and retried next interval |
| 6 | enter the read loop | — |

Log lines: `agent_worker_started` (consumer, domain, max concurrency), `agent_worker_stopping` (consumer, in-flight count).

## Read loop and concurrency

```text
while not stop_event:
    response = XREADGROUP agent-workers <consumer> {stream: ">"} count=<bound> block=5000
    for each entry:
        await semaphore.acquire()                    # blocks when full → entries stay pending for other workers
        task = create_task(_process_with_limit(...)) # releases the slot in `finally`
        track task
```

- `<bound>` = `AGENT_WORKER_MAX_CONCURRENCY` (default 10); the semaphore and the read count are the same number.
- The slot is acquired **before** the task exists. A full worker therefore claims nothing more.
- Each task is `process_request`, which always acknowledges (feature 003). `_process_with_limit` only owns the slot.
- The request id is bound per task (`contextvars`), so log lines of concurrent turns never mix.
- A turn holds no process-local state a sibling could corrupt.

## Shutdown (graceful)

On SIGTERM/SIGINT: `stop_event` is set. It is checked **between** reads, never inside the entry loop, so:

1. no new entries are read;
2. every task already created runs to completion and acknowledges (`await gather(in_flight)`);
3. the recovery loop is awaited (it returns promptly once the event is set);
4. the Redis client is closed;
5. `sql_store.close_pool()` then `close_checkpointer_pool()` (a pool left open past exit logs a "couldn't stop thread"
   warning).

**Bound**: a read in progress finishes within its 5 s block before the flag is noticed. The production compose file's
`stop_grace_period` is **90 s** for workers (**60 s** for the API) against a 60 s turn limit; a resume job is not
wrapped in that limit, so a long resume can outlast the grace period and be recovered by reclaim instead.

A worker that is **killed** skips all of the above; its claimed entries are recovered by another replica (feature 003).

## Settings that decide capacity

See data-model §7. The ones that bite first: `AGENT_WORKER_MAX_CONCURRENCY`, `REDIS_MAX_CONNECTIONS`,
`CHECKPOINTER_POOL_MAX_SIZE`, and the database's `max_connections` (data-model §6.3).

## Invariants a change must preserve

1. Acquire the slot before creating the task; never read more than the bound.
2. The stop flag is checked only between reads; a claimed job is never abandoned by a graceful stop.
3. One domain per process, fixed at startup; the domain is never carried in a job payload.
4. The checkpointer is opened on the loop that drives it.
5. Close every pool the process opened before exiting.
6. A new tunable lives in `Settings` **and** the example environment file (A3).
