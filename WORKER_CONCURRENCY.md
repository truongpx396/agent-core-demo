# Worker concurrency: how we decided, what we built

The reasoning behind scaling `app/job_queue/agent_worker.py` and `app/ingestion/ingest_worker.py` for concurrent requests, why both ended up on one concurrency model despite very different work, and how to size them. It's a scaling/ops decision record, so it lives apart from [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md).

## The starting question

`POST /ingest/upload` (`app/api/routers/ingest.py`) uploads to MinIO, publishes a job onto `ingest:requests` (`app/ingestion/ingest_queue.py`) and returns immediately; `app/ingestion/ingest_worker.py`, a separate consumer, does the parsing and embedding. `POST /chat/stream/queued` uses the same producer/consumer split against `app/job_queue/agent_worker.py` via `app/job_queue/queue.py`. The question: **if 50 requests land at once, do we need 50 worker containers?** No.

## Why 1:1 (requests : containers) is the wrong model

Both endpoints decouple "accepted" from "processed": 50 simultaneous requests are just 50 jobs on a Redis stream. The queue absorbs the burst, workers drain it at their sustainable rate, and nobody's HTTP request waits on a worker. So worker count is sized to the **real bottleneck**, which differs by worker:

- **`ingest_worker`:** CPU (PDF/DOCX extraction in `app/ingestion/extractors.py` is synchronous and CPU-bound), the embedding endpoint's rate limit, and Qdrant/MinIO throughput.
- **`agent_worker`:** the LLM backend's own real concurrency (see `AGENT_WORKER_MAX_CONCURRENCY` in `app/core/config.py`, and `loadtest/fake_llm_server.py`'s note on native Ollama's hard `-np 1` ceiling versus a genuinely concurrent backend).

Provisioning to the burst peak when the ceiling is elsewhere just pays for idle containers waiting on the same choke point.

## How to size worker capacity

1. **A fixed baseline for steady-state load,** not burst peaks: a handful of replicas.
2. **Autoscale on queue depth or consumer lag,** not request rate (`XLEN`/`XPENDING` on either stream): a growing backlog scales the service up (`docker compose --scale`, or an HPA equivalent), an empty queue scales it down. This reacts to *sustained* load, not a burst that clears in seconds. Nothing does this yet; it's the missing piece flagged in GRAPH_PATTERNS.md 43 (an orchestrator that scales on queue depth).
3. **A ceiling on scale-out** set by the real bottleneck (the embedding API's rate limit; the LLM backend's concurrency).
4. Users see "N requests accepted instantly, processed over the next seconds or minutes" (why both endpoints stream per-job progress over SSE).

## One process per container

Docker already gives independent restart-on-crash, resource accounting and trivial scaling *per container* (`restart: unless-stopped` is container-level, so a crashed process inside a multi-process container needs its own supervisor to be noticed at all). Both workers are one-process-per-container services in `docker-compose.yml`, scaled by replica count (`docker compose --profile app up -d --scale agent-worker=3` / `--scale ingest-worker=3`). Efficiency belongs *inside* the process (below), not in packing processes together. Each replica pushes its metrics over OTLP to one collector (GRAPH_PATTERNS.md 43), so scaling needs no scrape-config change either.

## Model 1: `agent_worker`, concurrency because turns mostly *wait*

`agent_worker.py::run()` runs up to `AGENT_WORKER_MAX_CONCURRENCY` turns in ONE process: an `asyncio.Semaphore` acquired *before* a task is created, then `asyncio.create_task` per job, not a serial `await` loop.

- **Why "50 parallel requests" doesn't need 50 cores:** a turn's slow part is a network round trip to an LLM API plus tool execution, during which the process is idle. asyncio overlaps many such waits on one thread: while turn A awaits the LLM, the loop runs turn B. That's **concurrency** (many outstanding I/O operations interleaved), not **parallelism**. If the "LLM call" were local CPU-bound computation, it wouldn't work (see Model 2).
- **Why it's safe:** a turn holds no in-process state a concurrent sibling could corrupt. Conversation state lives in Postgres via the checkpointer; tenant-scoped resources (Redis, Qdrant, the appdata pool) are per-call or pooled; and `bind_request_id`'s `contextvars.ContextVar` is copied per task by `asyncio.create_task`, so one task's request id never leaks into a sibling's logs.
- **The bottleneck it doesn't remove:** the checkpointer. `_open_checkpointer` (`app/agent/runtime/core.py`) backs `AsyncPostgresSaver` with a connection *pool* because psycopg wraps every operation on one connection in its own lock, so a single shared connection would serialize every turn's checkpoint reads and writes regardless of the semaphore. The pool is what lets `_MAX_CONCURRENCY` turns overlap.

## Model 2: `ingest_worker`, originally serial, now mirrors Model 1

It originally processed one job at a time (`_READ_COUNT = 1`, a sequential `await` loop). Not because jobs have unsafe shared state, but because download (`object_store.download_bytes`) and extraction (`extract_pdf_text`/`extract_docx_text`) were **synchronous blocking calls on the event loop thread**. Running several jobs via `asyncio.create_task` without wrapping them wouldn't have overlapped anything: the first job's extraction would hog the one thread, starving every sibling's I/O (including its own progress publishes). This is the mirror image of Model 1: turns are I/O-bound *and already async*; ingest jobs are partly CPU-bound *and were called synchronously*.

`ingest_worker.py` now matches `agent_worker.py`:

- **`INGEST_WORKER_MAX_CONCURRENCY`** (`app/core/config.py`, default `10`): the semaphore bound.
- **`run()`** acquires a slot *before* creating each job's task (so a full semaphore also backpressures reading from the stream), dispatches `asyncio.create_task(_process_with_limit(...))`, and tracks in-flight tasks so a graceful shutdown lets claimed jobs finish and ack instead of abandoning them.
- **`process_job`** wraps `object_store.download_bytes` and the extractor in `asyncio.to_thread` (on top of `ingestor.ingest_text`, which already ran that way). This is *what makes* `_MAX_CONCURRENCY > 1` real here; without it, concurrency would be cosmetic.

### A gap only Model 2 has: the thread pool `to_thread` borrows from

`agent_worker.py` touches no thread pool (every turn is awaited async I/O), so `AGENT_WORKER_MAX_CONCURRENCY` is the only limit. `ingest_worker.py`'s `to_thread` calls borrow from the event loop's **default executor**, sized by Python as `min(32, os.cpu_count() + 4)` and unrelated to `INGEST_WORKER_MAX_CONCURRENCY`. That's a silent ceiling: raising the setting past 32 would admit more jobs than the pool can serve, the excess quietly queueing for a thread; and below 32, whether the default lands above or below the setting is an accident of the host's core count.

`run()` now sets the executor explicitly at startup: `loop.set_default_executor(ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY, ...))`. Exactly `_MAX_CONCURRENCY`, not padded by core count: a job's `to_thread` calls are sequential (download, extract, embed), so one in-flight job never holds more than one pool thread. Core count is left out on purpose: the GIL, not thread count, bounds CPU-bound throughput, and `_MAX_CONCURRENCY` is already the operator's CPU-vs-I/O tuning knob; folding core count back in would silently override it.

### The honest limitation Model 2 still has

`asyncio.to_thread` moves a blocking call off the event loop onto a real OS thread, but the GIL still serializes CPU-bound bytecode across threads. When extraction is the bottleneck (a huge PDF), several concurrent extractions get no parallel CPU throughput; they only overlap the *I/O* of sibling jobs (MinIO downloads, embedding HTTP calls, Qdrant upserts, progress publishes). Model 1 has no such step: an LLM call isn't local CPU work. For CPU-dominated ingest, more **replicas** add real parallelism; `_MAX_CONCURRENCY` is the lever when jobs spend more time waiting on MinIO, the embedding endpoint or Qdrant than parsing. They compose: N replicas × `_MAX_CONCURRENCY`.

## Sizing the two knobs

Neither comes from a formula:

- **`AGENT_WORKER_MAX_CONCURRENCY`** (default `10`) depends on the LLM backend's real concurrency; a load-test run dials it without a redeploy (see the setting's comment in `app/core/config.py`).
- **`INGEST_WORKER_MAX_CONCURRENCY`** (default `10`, matching the agent worker as a starting point, not a measured optimum) depends on how I/O-dominated the real document mix is. Raise it if profiling shows a worker idle-waiting on MinIO, the embedding endpoint or Qdrant more than extracting; lower it, and add replicas, if extraction dominates.

## Postgres connection budget

Scaling replicas multiplies Postgres connections, and nothing enforces the ceiling. Every process that runs the graph (each `agent-worker` replica **and** the API, via `init_graph_async()` in its lifespan) can hold two pools: the appdata pool (`app/core/sql_store.py`, `max_size=10`, not configurable today) and the checkpointer pool (`CHECKPOINTER_POOL_MAX_SIZE`, default `10`). Worst case:

    20 × (agent-worker replicas + API replicas)  ≤  max_connections − reserved

Postgres's default `max_connections` is `100` (3 reserved for superusers), which fits only about four such processes before counting anything else on the server. `docker-compose.prod.yml` doesn't set it, and that Postgres also serves LiteLLM and Langfuse, so a scaled-out deploy should raise it (or put PgBouncer in front) rather than discover the limit under load.

**What exhaustion looks like:** pool checkouts fail with `FATAL: sorry, too many clients already`, turns fail *inside* an already-open SSE stream, and the client sees HTTP 200 with no answer text. That exact failure took down `tests/integration/test_worker_scaling.py`'s 250-turn test (245 of 250 empty) until `tests/containers.py::ensure_postgres` got `max_connections=300` (5 workers + the API alone is 6 × 20 = 120 potential connections).

## A different knob: the UI's per-request file cap

`MAX_UPLOAD_FILES_PER_REQUEST` (`app/core/config.py`, default `5`, enforced in `POST /ingest/upload` and mirrored in `app/api/static/index.html`'s upload form) caps how many files ONE submission may batch. It's a UX/abuse guard on one HTTP request, not a worker setting: a user can submit several batches back to back, and all `ingest-worker` replicas × `INGEST_WORKER_MAX_CONCURRENCY` drain them concurrently however they were batched. Raising the cap adds no ingest throughput, and raising `INGEST_WORKER_MAX_CONCURRENCY` doesn't change how many files one form may include.
