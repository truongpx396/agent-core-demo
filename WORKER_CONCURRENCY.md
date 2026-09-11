# Worker concurrency: how we decided, what we built

This documents the reasoning behind scaling `app/turns/agent_worker.py` and
`app/ingestion/ingest_worker.py` for concurrent requests, why they ended up
sharing one concurrency model despite doing very different work, and the
decision framework behind it. Written up separately from
[GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) because it's a scaling/ops decision
record, not a description of a graph node or tool.

## The starting question

`POST /ingest/upload` (`app/api/main.py`) is fire-and-forget: it uploads to
MinIO and publishes a job onto `ingest:requests`
(`app/ingestion/ingest_queue.py`), then returns immediately. The actual
parsing/embedding happens in `app/ingestion/ingest_worker.py`, a separate
consumer process — the same producer/consumer split
`POST /chat/stream/queued` uses against `app/turns/agent_worker.py` via
`app/turns/queue.py`. The question that started this: **if 50 requests land
at once, does that require 50 running worker containers?**

Short answer: no. Longer answer below, because the reasoning generalizes
past this one number, and differs in one important way between the two
workers.

## Why 1:1 (requests : containers) is the wrong model

Both producer endpoints already decouple "accepted" from "processed" — 50
simultaneous requests just means 50 jobs land on a Redis stream almost
instantly, regardless of how many workers exist. The queue absorbs the
burst; workers drain it at whatever rate they can sustain. Nobody's HTTP
request blocks on worker availability — that's the point of putting a queue
between the API and the actual work.

So worker count isn't sized to "peak concurrent requests." It's sized to
the **real bottlenecks**, which differ by worker:

- For `ingest_worker`: **CPU** (PDF/DOCX extraction,
  `app/ingestion/extractors.py`, synchronous and CPU-bound), the embedding
  endpoint's own rate limit, and Qdrant/MinIO throughput.
- For `agent_worker`: the **downstream LLM backend's own real concurrency**
  — see `AGENT_WORKER_MAX_CONCURRENCY`'s own docstring in
  `app/core/config.py`, and `loadtest/fake_llm_server.py`'s note on native
  Ollama's hard `-np 1` ceiling vs. a genuinely concurrent backend.

Provisioning to the burst peak (50 containers) when the real ceiling is
somewhere else just burns money on idle containers waiting on the same
choke point.

## How a pro team sizes worker capacity

1. **Fixed baseline sized to steady-state load**, not burst peaks — a
   handful of replicas normally.
2. **Autoscale on queue depth/consumer lag**, not on request rate. Redis
   Streams exposes this via `XLEN`/`XPENDING` on either stream. A growing
   backlog scales the worker service up (`docker compose --scale`, or an
   HPA-equivalent); an empty queue scales it back down. This reacts to
   *sustained* load rather than overprovisioning for a burst that clears
   itself in seconds. (GRAPH_PATTERNS.md pattern 43's own "Extending
   Further" note flags this as the still-missing piece: an actual
   orchestrator that scales replica count on queue depth rather than CPU.)
3. **A ceiling on scale-out**, capped by the real bottleneck (e.g. don't
   scale `ingest-worker` past what the embedding API's own rate limit can
   usefully absorb, or `agent-worker` past the LLM backend's own
   concurrency).
4. Users see "N requests accepted instantly, processed over the following
   seconds/minutes" — acceptable, expected UX for anything queued (why both
   `/chat/stream/queued` and `POST /ingest/upload` stream progress over SSE
   per job rather than blocking on the response).

## One process per container, not several processes per container

Docker/container orchestration already gives independent restart-on-crash,
independent resource accounting, and (via `--scale`) trivial horizontal
scaling *per container*. Packing several worker processes into one
container throws that away — Docker's `restart: unless-stopped` is a
container-level policy, so a crashed process inside a multi-process
container needs its own supervisor to be noticed and restarted at all.
Both workers are deployed as one-process-per-container services in
`docker-compose.yml`, scaled via replica count (`docker compose --profile
app up -d --scale agent-worker=3` / `--scale ingest-worker=3`), and that
stays the right shape — the fix for "efficiency" belongs *inside* the
process (below), not in how many processes share a container. Each
replica's metrics push via OTLP to one aggregated collector (GRAPH_PATTERNS.md
pattern 43), so scaling replica count needs no scrape-config change either.

## Model 1: `agent_worker` — concurrency because turns mostly *wait*

`app/turns/agent_worker.py::run()` runs up to `AGENT_WORKER_MAX_CONCURRENCY`
turns at once in ONE process — an `asyncio.Semaphore` acquired *before* a
task is created, then `asyncio.create_task` per job, not a serial `await`
loop.

**Why "50 parallel requests" doesn't mean 50 CPU cores working at once:** a
turn's slow part is a network round trip to an LLM API plus tool execution
— the process is idle during that wait. asyncio's whole value proposition
is overlapping many such waits on one thread: while turn A is blocked on
the LLM's response, the event loop is free to run turn B's code. That's
**concurrency** (many outstanding I/O operations interleaved on one
thread), not **parallelism** (multiple cores computing simultaneously). If
the "LLM call" were instead local, CPU-bound computation, this trick
wouldn't work at all — see Model 2 below for exactly that failure mode.

**Why it's safe:** a turn holds no in-process state a concurrent sibling
could corrupt. Conversation state lives in Postgres via the checkpointer,
not in the process; tenant-scoped resources (Redis, Qdrant, the appdata
pool) are already per-call/pooled; and `bind_request_id`'s
`contextvars.ContextVar` is copied per-task by `asyncio.create_task` rather
than shared, so one task's request id never leaks into a sibling's log
lines.

**The bottleneck this doesn't remove:** the checkpointer itself has a
fine-grained serialization point — `_open_checkpointer` in
`app/agent/runtime.py` backs `AsyncPostgresSaver` with a connection *pool*
specifically because psycopg wraps every operation on one connection in its
own internal lock, so a single shared connection would serialize every
concurrent turn's checkpoint reads/writes regardless of the semaphore. The
pool is what actually lets `_MAX_CONCURRENCY` turns overlap; the semaphore
alone wouldn't have been enough.

## Model 2: `ingest_worker` — originally serial, now mirrors Model 1

`app/ingestion/ingest_worker.py` originally processed exactly one job at a
time per process (`_READ_COUNT = 1`, a plain sequential `await` loop). Not
because ingest jobs have unsafe shared state — they don't, same as turns —
but because download (`object_store.download_bytes`) and extraction
(`extract_pdf_text`/`extract_docx_text`) were **synchronous blocking calls
run directly on the event loop thread**, never wrapped in
`asyncio.to_thread`. Running several such jobs "concurrently" via
`asyncio.create_task` without that wrapping wouldn't have overlapped
anything — the first job's blocking extraction call would hog the one
thread the event loop runs on, starving every sibling job's I/O (including
its own progress publishes) until it finished. This is the mirror image of
why Model 1 works: turns are I/O-bound *and already async*; ingest jobs are
partly CPU-bound *and were called synchronously*.

`ingest_worker.py` now mirrors `agent_worker.py`'s concurrency model
exactly:

- **`INGEST_WORKER_MAX_CONCURRENCY`** (`app/core/config.py`, default `10`,
  env-configurable) — the semaphore bound, same shape and same default as
  `AGENT_WORKER_MAX_CONCURRENCY`.
- **`run()`** acquires a semaphore slot *before* creating each job's task
  (so a full semaphore backpressures reading off the stream too, not just
  task creation), dispatches via `asyncio.create_task(_process_with_limit(...))`,
  and tracks in-flight tasks so a graceful shutdown lets already-claimed
  jobs finish and ack rather than abandoning them mid-job — byte-for-byte
  the same shape as `agent_worker.py::run()`.
- **`process_job`** now wraps `object_store.download_bytes` and the
  extractor call in `asyncio.to_thread`, on top of `ingestor.ingest_text`
  (which already ran that way, for progress-event reasons — see the
  module's own docstring). This is *what actually makes*
  `_MAX_CONCURRENCY > 1` safe here — without it, concurrency would be
  purely cosmetic, same failure mode described above.

## A gap Model 2 has that Model 1 never did: the thread pool `to_thread` borrows from

`agent_worker.py` never touches a thread pool at all — every turn is pure
async I/O (an async LLM client, awaited directly), so `AGENT_WORKER_MAX_CONCURRENCY`
is the only concurrency limit that exists. `ingest_worker.py` is different:
its `asyncio.to_thread` calls borrow worker threads from the event loop's
**default executor**, and by default that's a `concurrent.futures.ThreadPoolExecutor`
sized by Python itself — `min(32, os.cpu_count() + 4)` — created lazily on
first use, with zero relationship to `INGEST_WORKER_MAX_CONCURRENCY`.

That's a real, silent ceiling: it's hard-capped at 32 threads regardless of
host, so raising `INGEST_WORKER_MAX_CONCURRENCY` past 32 (a legitimate move
for a genuinely I/O-dominated deployment, per this doc's own sizing
guidance) would have the semaphore admit more concurrent jobs than the pool
can actually serve — the excess would silently queue for a thread instead
of running, quietly capping real concurrency below the number someone
deliberately configured. And below 32, whether the default lands above or
below `_MAX_CONCURRENCY` is just an accident of `os.cpu_count()` on
whatever host happens to run it — not a decision anyone made.

`run()` now sets the executor explicitly instead:
`loop.set_default_executor(ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY, ...))`,
called once at startup. The size is exactly `_MAX_CONCURRENCY`, not padded
by CPU core count: a job's `to_thread` calls are sequential (download, then
extract, then embed), never overlapping each other, so one in-flight job
never holds more than one pool thread at a time — `_MAX_CONCURRENCY`
concurrent jobs need at most `_MAX_CONCURRENCY` pool threads, exactly.
Core count is deliberately left out of the formula: the GIL, not thread
count, is what bounds CPU-bound throughput regardless of how many cores
exist (see the limitation below), and `_MAX_CONCURRENCY` is already the
operator's own CPU-vs-I/O-aware tuning knob — folding core count back into
the executor size would just silently override that deliberate choice, the
same failure mode this fix removes.

## The honest limitation Model 2 still has, that Model 1 doesn't

`asyncio.to_thread` moves a blocking call off the event loop thread onto a
real OS thread — but Python's GIL still serializes CPU-bound bytecode
execution across threads. For a document whose extraction step is the
actual bottleneck (a huge PDF), running several extractions "concurrently"
via threads doesn't get genuine parallel CPU throughput; it gets overlap on
the *I/O* portions of sibling jobs (their own MinIO downloads, embedding
HTTP calls, Qdrant upserts, progress publishes) while that one extraction
runs. Model 1 doesn't have this problem because an LLM call isn't local CPU
work at all — there's no GIL-bound step to serialize.

For a CPU-bound-dominated ingest workload, more **worker
processes/replicas** is still the lever that adds real parallelism;
`_MAX_CONCURRENCY` is the right lever when jobs spend more wall-clock time
waiting on MinIO/the embedding endpoint/Qdrant than on local parsing. In
practice both levers compose: N replicas × `_MAX_CONCURRENCY` each.

## Sizing the two concurrency knobs

Neither is derived from a fixed formula:

- **`AGENT_WORKER_MAX_CONCURRENCY`** (default `10`) — depends on the
  downstream LLM backend's own real concurrency; a load-testing run dials
  it without a redeploy rather than this app picking one value and
  forgetting it (see the setting's own comment in `app/core/config.py`).
- **`INGEST_WORKER_MAX_CONCURRENCY`** (default `10`, matching the agent
  worker's default as a starting point, not a measured optimum) — depends
  on how I/O-dominated the real document mix is relative to CPU-bound
  parsing time. Raise it if profiling shows a worker process spending more
  time idle-waiting on MinIO/the embedding endpoint/Qdrant than on
  extraction; lower it, and lean on replica count instead, if extraction
  dominates.

## A related-but-different knob: the UI's per-request file cap

`MAX_UPLOAD_FILES_PER_REQUEST` (`app/core/config.py`, default `5`, enforced
in `POST /ingest/upload`, mirrored client-side in
`app/api/static/index.html`'s upload form) caps how many files ONE browser
submission may batch into a single `POST /ingest/upload` call. This is a
UX/abuse guard on one HTTP request, not a worker concurrency setting — a
user can still submit several batches back to back, and however many
`ingest-worker` replicas × `INGEST_WORKER_MAX_CONCURRENCY` exist will drain
all of them concurrently regardless of how the jobs were batched into
requests. Don't confuse the two: raising this cap doesn't add ingest
throughput, and raising `INGEST_WORKER_MAX_CONCURRENCY` doesn't change how
many files one form submission may include.
