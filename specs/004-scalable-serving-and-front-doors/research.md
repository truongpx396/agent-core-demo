# Research: Scalable Serving and Front Doors

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments, `WORKER_CONCURRENCY.md` and
`GRAPH_PATTERNS.md` patterns 29, 42, 43 and 49. Each entry names its evidence. **No `NEEDS CLARIFICATION` remains.**
R19–R23 (Part E) are *findings* from verifying the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**. *Alternatives are those the code or
its docs name or argue against; where none is recorded the entry says so rather than inventing one.*

---

## Part A — Topology and scaling

### R1. A queue sits between the HTTP tier and the agent

- **Decision**: `POST /chat/stream/queued` publishes a job onto a Redis Stream and relays the job's results stream
  as SSE. The HTTP tier never runs the graph for chat; it is "the ONLY HTTP chat path this app serves".
- **Rationale**: Idle browser tabs are cheap to hold; a worker doing real model work is expensive to hold.
  Scaling both on one axis (an in-process endpoint) wastes whichever is cheaper. The split also means the HTTP
  tier keeps answering liveness, readiness and read requests while workers are saturated.
- **Alternatives considered**: an in-process streaming endpoint (it existed first; it was removed once the queued
  one covered everything, which is why some comments still name it — A5); a thread pool inside the API process
  (rejected: ties the two scaling axes together).
- **Evidence**: `app/job_queue/queue.py` module docstring; `app/api/main.py::chat_stream_queued`; pattern 43.

### R2. One stream and consumer group per domain; the domain is inferred, never carried

- **Decision**: `requests_stream_key(domain)` = `agent:requests:<domain>`; each worker process reads exactly one
  (`AGENT_DOMAIN`); the API validates `X-Domain` against the registry and routes by it. A resume or cancel must
  land on the same domain's stream as the original turn.
- **Rationale**: The paused graph was compiled from that domain's manifest and tools, so another domain's worker
  resuming it would run the wrong tool universe. An unknown `X-Domain` is a 422 rather than a publish onto a stream
  nothing reads, which would hang silently.
- **Alternatives considered**: one flat stream with `domain` in the payload (the original shape — it left the web
  UI able to reach only whichever domain a pool happened to serve); a worker serving several domains (a README
  Roadmap item: it needs per-domain graphs in one process).
- **Evidence**: pattern 49; `queue.py::requests_stream_key`; `app/api/main.py::get_domain`;
  `tests/api/test_api.py::TestChatStreamQueued::test_a_non_ecorp_domain_publishes_onto_its_own_stream`.

### R3. One process per container; scale by replicas

- **Decision**: Each worker is one process in one container, scaled with `--scale agent-worker=N`; the efficiency
  fix lives *inside* the process (R5), not in packing processes into a container.
- **Rationale**: Container orchestration already gives independent restart, resource accounting and trivial
  horizontal scaling per container; several processes in one container would need their own supervisor.
- **Alternatives considered**: multi-process containers (rejected, as above).
- **Evidence**: `WORKER_CONCURRENCY.md` ("One process per container"); `docker-compose.yml`,
  `docker-compose.prod.yml`; the `Dockerfile` header ("one image, three roles").

### R4. Size to the real bottleneck, not to the burst

- **Decision**: 50 simultaneous requests are 50 jobs on a stream, absorbed instantly regardless of worker count.
  Worker count follows steady-state load and the *downstream* ceiling (the model backend's real concurrency; for
  ingest, CPU and the embedding endpoint). Autoscaling on queue depth is the intended next step and is **not built**.
- **Rationale**: Provisioning to the peak burns money on idle containers waiting on the same choke point. A native
  Ollama serves one generation at a time, so an app load test against it measures Ollama.
- **Alternatives considered**: one container per request (rejected); autoscale on request rate or CPU (rejected in
  favor of queue depth/consumer lag, which reflects sustained load rather than a burst that clears).
- **Evidence**: `WORKER_CONCURRENCY.md` ("How a pro team sizes worker capacity"); `loadtest/fake_llm_server.py`.

### R5. Per-process concurrency is a semaphore acquired before the task is created

- **Decision**: `run()` acquires a slot, *then* `asyncio.create_task(_process_with_limit(...))`; the task releases
  the slot in a `finally`. `_READ_COUNT` equals the bound, so a single read never claims more than the process will
  start.
- **Rationale**: Acquiring before creating backpressures *reading*: a full worker claims nothing and the entries
  stay pending for another worker, instead of piling up in memory inside this one.
- **Alternatives considered**: a serial `await` loop (one turn at a time — what ingest used to do); `create_task`
  with no bound (unbounded memory and connection use).
- **Evidence**: `app/job_queue/agent_worker.py::run`, `_process_with_limit`;
  `tests/job_queue/test_agent_worker.py::TestConcurrentDispatch`.

### R6. Concurrency is safe because a turn holds no process-local state

- **Decision**: Conversation state lives in Postgres via the checkpointer; per-call resources (Redis, Qdrant, the
  appdata pool) are pooled; `bind_request_id`'s `contextvars.ContextVar` is copied per task by `create_task`.
- **Rationale**: Nothing a sibling turn could corrupt lives in the process. The remaining serialization point was
  the checkpointer's own lock: `AsyncPostgresSaver` guards every checkpoint operation with one `asyncio.Lock` per
  saver, which hard-serialized checkpoint I/O even with a pool (measured ~5× latency growth from 1 to 50
  concurrent turns). `runtime.py` swaps it for `asyncio.Semaphore(CHECKPOINTER_POOL_MAX_SIZE)` — a workaround for
  langgraph#7259, to be removed when the fix (#7269) ships; it touches a private attribute, so it is re-checked
  before any `langgraph-checkpoint-postgres` bump.
- **Alternatives considered**: leaving the lock (rejected: the measured growth); forking the library (rejected).
- **Evidence**: `app/agent/runtime.py::_open_checkpointer` docstring; the installed saver's `_cursor`
  (`async with self.lock, …`, confirmed 2026-10-02); `WORKER_CONCURRENCY.md` ("Model 1").

### R7. The Redis client is configured for long blocking reads and many held connections

- **Decision**: `socket_timeout=None` (a blocking `XREAD`/`XREADGROUP` is bounded by its own server-side `BLOCK`,
  5000 ms); `socket_connect_timeout` left at its default so an unreachable Redis still fails fast;
  `max_connections=REDIS_MAX_CONNECTIONS` (300).
- **Rationale**: redis-py's 5 s default socket timeout raced the 5000 ms block, raising `TimeoutError` before Redis's
  own block elapsed — a real bug, guarded by a regression test. Separately, every SSE connection holds a pooled
  connection for the whole turn, so the library's default 100 was exhausted by the first real load test:
  250 concurrent requests failed ~83% of the time with `MaxConnectionsError`, while every request that *did* get a
  connection completed correctly — only running past the old ceiling surfaced it.
- **Alternatives considered**: per-request connections (rejected: connection churn); one multiplexed reader
  (rejected: much more machinery than a larger pool).
- **Evidence**: `queue.py::get_client` docstring; the `socket_timeout` choice is pinned by
  `tests/job_queue/test_queue.py::TestGetClient`; the `max_connections` choice is guarded **only** by
  `tests/integration/test_worker_scaling.py` (no hermetic test asserts it).

### R8. The Postgres connection budget is documented because nothing enforces it

- **Decision**: Every process that runs the graph — each worker replica *and* the API — may hold two pools: appdata
  (`max_size=10`, not configurable) and the checkpointer (`CHECKPOINTER_POOL_MAX_SIZE`, 10). The worst case is
  written down: `20 × (workers + API replicas) ≤ max_connections − reserved`. Postgres's default of 100 fits about
  four such processes, before LiteLLM and Langfuse that share the server.
- **Rationale**: Exhaustion is easy to mistake for something else: checkouts fail with `FATAL: sorry, too many
  clients already`, turns fail *inside* an already-open SSE stream, and the client sees HTTP 200 with no answer
  text. This took down the 250-turn test (245 of 250 empty) until the test container was given
  `max_connections=300`.
- **Alternatives considered**: a pooler in front (PgBouncer — recommended in the doc, not built); enforcing the
  bound in code (rejected: it depends on a database setting the app cannot see).
- **Evidence**: `WORKER_CONCURRENCY.md` ("Postgres connection budget"); `tests/containers.py::ensure_postgres`;
  `app/agent/sql_store.py` (pool construction).

---

## Part B — Process lifecycle

### R9. Start in an order that fails loud and sees the backlog

- **Decision**: `run()` resolves the domain first (an unknown `AGENT_DOMAIN` raises with the valid names), opens
  the checkpointer on *this* process's loop, creates the consumer group with `id="0"`, then installs signal
  handlers, starts the recovery loop, and reads.
- **Rationale**: A typo must not silently serve the wrong domain. `id="0"` means a worker that starts after the API
  still sees requests that arrived first. The checkpointer's lock binds to the loop that first uses it, so it must
  be opened on the loop that will drive it.
- **Alternatives considered**: falling back to the default domain on a typo (rejected: a confusing failure mode);
  `id="$"` (rejected: skips earlier jobs).
- **Evidence**: `agent_worker.py::run`; `domains/registry.py::resolve_domain`; `queue.py::ensure_consumer_group`;
  `runtime.py` module docstring.

### R10. A stop signal sets a flag; it never kills the loop mid-read

- **Decision**: SIGTERM/SIGINT set `stop_event`. It is checked *between* `xreadgroup` calls, never inside the entries
  loop, so every claimed job runs to completion and acknowledges. Then the recovery loop is awaited, the Redis client
  closed, and both Postgres pools closed (a pool left open past exit prints a "couldn't stop thread" warning).
  The production compose file gives workers a 90 s stop grace period and the API 60 s.
- **Rationale**: A redeploy is the most frequent "failure" a worker sees. If it abandoned in-flight turns, every
  release would be a burst of lost-worker errors. A *killed* worker is a different case, handled by feature 003's
  reclaim.
- **Alternatives considered**: cancelling in-flight tasks (not chosen: it would abandon turns mid-write). Whatever is
  still running when the grace period ends is killed by the orchestrator and recovered by feature 003's reclaim.
- **Evidence**: `agent_worker.py::run` comments; `docker-compose.prod.yml` (`stop_grace_period`). **Not tested** (A1).

### R11. Telemetry is configured in the API's lifespan, not at import

- **Decision**: `configure_logging()` runs at import; `configure_telemetry("agent-core-api")` runs in the lifespan,
  next to opening the checkpointer.
- **Rationale**: OpenTelemetry's `set_meter_provider` is call-once; at module level, whichever test module imports
  the API first would win that race. The lifespan never runs under the hermetic suite.
- **Evidence**: `app/api/main.py::lifespan` comment; `tests/api/test_api.py` module docstring.

---

## Part C — Front doors

### R12. One rulebook behind every door

- **Decision**: The web/HTTP path goes through the queue to a worker running `astream_events_turn`; the terminal runs
  `astream_events_turn` in-process; the chat app runs `astream_events_turn_unattended` in-process. All three reach the
  same graph and the same approval gate.
- **Rationale**: A second interface that relaxed a rule would be an unreviewed back door. Doors differ only in the
  two things that genuinely differ: where identity comes from and who approves.
- **Alternatives considered**: a chat-app path through the queue (not done; it would add a hop for a front door that
  needs one final reply, not a stream).
- **Evidence**: `app/channels/chat.py`, `app/channels/telegram.py`, `app/job_queue/agent_worker.py`.

### R13. The built-in page is one self-contained file that speaks the published contract

- **Decision**: A single HTML file (inline CSS and JS, no build step, no CDN) served at `GET /`. It uses `fetch()`
  and parses SSE frames by hand rather than `EventSource`. It sends the identity and domain headers from editable
  selectors and starts a fresh conversation when any of them changes. It renders a draft that is promoted to the
  answer only when confirmed, so a `retry` discards only the draft.
- **Rationale**: `EventSource` cannot send custom headers or POST, and the queued endpoint needs both. Keeping the page
  on the published vocabulary means it can never depend on a private route. A fresh conversation on identity change
  exists because a conversation belongs to the identity that started it (feature 002).
- **Alternatives considered**: a framework app with a build pipeline (rejected: the demo should run with nothing);
  `EventSource` with a query-string identity (rejected: identity in a URL).
- **Evidence**: pattern 29; `app/api/static/index.html` (`pumpSSE`, `createTurnHandlers`, `startFreshConversation`);
  `tests/api/test_api.py::TestUi`; `tests/live/test_chat_ui.py`.

### R14. The terminal is a thin in-process client under a local identity

- **Decision**: `chat.py` initializes the checkpointer on its loop, uses tenant `DEFAULT_TENANT` and principal
  `local:<os user>`, renders `token`/`tool_start`/`tool_end`/`approval_required`/`error`/`done`, and — whenever a run
  pauses — asks `Approve? [y/N]` and resumes. `--hitl` additionally gates *every* tool call.
- **Rationale**: This process *is* the trust boundary (no network hop, no untrusted client), unlike the HTTP headers.
  The OS user as principal gives people sharing a machine separate memories. Because the mandatory gate pauses any
  mutating call regardless of the flag, the prompt loop runs without `--hitl` too.
- **Alternatives considered**: a REPL over the HTTP API (rejected: needs the stack; the in-process path is what
  makes it a no-infrastructure demo).
- **Evidence**: `app/channels/chat.py`. Its gaps: it ignores `citations`, `followups`, `system_note` and `retry`
  (A2, B6).

### R15. The chat-app channel long-polls, persists its position after handling, and is unattended

- **Decision**: `getUpdates` long-poll (30 s window); each text message → one thread per chat
  (`telegram:<chat_id>`), one principal per sender (`telegram:<user_id>`) in the default tenant; the turn runs through
  `astream_events_turn_unattended`; the reply is the collected tokens plus a "Sources" list, split at 4000 characters;
  the position (`telegram:offset:<domain>` in Redis) is persisted **after** each message is handled; messages are
  handled strictly one at a time; non-text updates are skipped but still advanced past.
- **Rationale**: Long polling needs no inbound port or public URL (right for local/demo); a real deployment would
  switch to a webhook. Persisting *after* handling makes the failure mode "one duplicate reply" rather than "a silently
  dropped message" — the right trade for a chat bot. Persisting the position at all closes a real bug: it used to be a
  local variable, so every restart reset it to 0 and Telegram redelivered every update it still remembered, each
  producing a duplicate turn and reply to a real user. Sequential handling is a stated demo-scope choice.
- **Alternatives considered**: persist before handling (rejected: drops a message if the process dies while
  handling); a webhook (not built — no verifiable deployment); a worker pool of concurrent chats (not built).
- **Evidence**: `app/channels/telegram.py`; pattern 42; `tests/channels/test_telegram_channel.py`
  (`TestOffsetPersistence`, `TestRunPersistsOffsetAcrossPolls`, `TestHandleMessage`, `TestSendMessage`).

---

## Part D — Verification

### R16. Capacity is demonstrated with real processes, against a model stand-in

- **Decision**: `tests/integration/test_worker_scaling.py` starts real Postgres and Redis, a real HTTP server and N real
  worker processes, then fires concurrent real HTTP requests and checks every answer is *correct* — not merely that
  nothing crashed. Three scenarios: 250 concurrent calculator turns; concurrent approve round trips that write to
  Qdrant; concurrent delegation runs whose nested model call goes over the wire. The model is
  `loadtest/fake_llm_server.py`, which, unlike a native Ollama, answers many requests at once.
- **Rationale**: The in-process concurrency tests prove the mechanism at small scale. Only real processes and sockets
  expose a ceiling nobody configured (R7, R8). It needs Docker but no model, so it runs in the `integration` CI job.
- **Alternatives considered**: reasoning about capacity (rejected: the first real run falsified it); testing against a
  real model (rejected: measures the model).
- **Evidence**: `tests/integration/test_worker_scaling.py`, `loadtest/fake_llm_server.py`.

### R17. A Locust suite covers every queued endpoint and the failure paths

- **Decision**: `loadtest/locustfile_queued.py` — plain, cached and long-history turns; approve and reject round trips;
  cancel mid-stream; a resume with nothing paused (the `checkpoint_lost` trigger); an invalid identity; the per-tenant
  rate limit; upload. It is built so a run moves the panels of the overview dashboard. Metrics it *cannot* reach
  (cost-ceiling trips at a $0 model price, no-progress and invalid-tool-call guards that need a misbehaving model, an
  unreachable memory-deletion path, the outward-tool gate) are listed in its header, each checked against the source.
- **Rationale**: Throughput numbers alone do not show whether the guardrails and degraded paths register.
- **Alternatives considered**: a throughput-only benchmark (rejected: see above).
- **Evidence**: `loadtest/locustfile_queued.py`; Makefile `loadtest-*`. It never sets `X-Domain` (A8).

---

## Part E — Findings (not decisions)

### R18. FINDING B5 — a turn that raises ends the chat-app channel

- **Observation**: `run()` guards the *poll* (`except Exception … sleep(5)`) but calls `handle_message` and
  `_save_offset` unguarded. `handle_message` → `_run_turn` iterates `astream_events_turn_unattended`, which normally
  turns a failure into an `error` event but can raise from code that runs before its own error handling. A raise
  propagates out of the loop.
- **Reproduction**: a fake HTTP client serving one text update and a turn that raises → `run()` exits with the
  exception; the persisted position is unset; a second `run()` polls the same offset again. (Scratch harness; not in
  the suite.)
- **Consequence**: every chat served by the process stops; with no restart policy for the channel (A9) it stays down.
  If the failure is deterministic for one message it is a crash loop. Safety is unaffected — nothing writes without
  approval.
- **Options**: guard each message (log + count + generic reply + advance the position); bound re-handling if the
  position must not advance. Left open and disclosed.

### R19. FINDING B6 — two clients ignore `retry`

- **Observation**: Feature 001's contract: a client MUST discard what it has rendered when `retry` arrives. `index.html`
  does; `telegram.py::_run_turn` collects every `token` and ignores `retry`; `chat.py::_render_stream` prints tokens
  immediately and ignores it.
- **Reproduction**: feed `token("…90 days. ")`, `retry`, `token("…30 days.")`, `done` to each → both produce
  `"…90 days. …30 days."`.
- **Consequence**: the rejected draft — which the quality gate judged bad — is delivered with the corrected answer.
  Worse for the chat app, which sends one final message; the terminal has already printed the draft.
- **Options**: reset the accumulator on `retry` (chat app); buffer or print a separator (terminal). Left open.

### R20. FINDING A3 — most settings have no example-environment entry

- **Observation**: The constitution (*Configuration*) and `CLAUDE.md` require every tunable to be in `Settings` *and*
  `.env.example`. Of 61 `Settings` fields, 33 appear in neither `.env.example` nor `.env.prod.example`, including every
  queue and worker tunable (`agent_worker_max_concurrency`, `redis_max_connections`, `chat_first_response_deadline_seconds`,
  the reclaim settings, `checkpointer_pool_max_size`). Feature 003's task T005 said they had entries.
- **Consequence**: the settings that decide capacity are discoverable only by reading `config.py`.

### R21. FINDING A4 — the image health check is wrong for the worker role

- **Observation**: the `Dockerfile` defines one `HEALTHCHECK` against the API's port. The worker services use the same
  image and no compose file overrides the check, so the check runs in a container that never opens that port. The
  Dockerfile's own comment acknowledges the check is for the default role only.
- **Consequence (by reading; not run)**: the workers would be reported unhealthy; an orchestrator that acts on health
  would restart them. They also have no health signal of their own, and there is no queue-depth gauge or dead-letter alert.

### R22. FINDING A5 — comments and a pattern describe superseded behavior

- **Observation**: the `checkpointer_pool_max_size` comment (one turn's checkpoint I/O at a time), the `agent_worker.py`
  docstring (a direct `POST /chat/stream`), and pattern 43 (no redelivery, one flat stream, eager results delete) each
  contradict code that changed later. Governance: the conflicting document MUST be corrected.

### R23. FINDING A8 — only the default domain is tested under load

- **Observation**: neither the scaling test nor the Locust file sets `X-Domain` or starts a non-default worker.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B5 | Guard each chat-app message; reply, count and advance | Needs the "advance or bound" decision; test first |
| B6 | Honor `retry` in the chat app and the terminal | Terminal needs a buffering/separator decision; test first |
| A1 | Test `run()`'s graceful stop | Cheap with the fake Redis |
| A2 | Test the terminal; render sources and follow-ups | Small |
| A3 | Add the missing example-environment entries; a test to keep them in sync | Needs an allow-list decision |
| A4 | Disable the image health check for workers; add a depth gauge and a dead-letter alert | Needs a definition of a healthy worker |
| A5 | Correct the three stale texts | Docs-only; do first |
| A6 | Count failed chat-app sends | Small |
| A7 | A hermetic harness for the page's event handling | Weigh a JS test dependency |
| A8 | A non-default-domain pool in the scaling test and the load test | Needs Docker |
| A9 | A compose service for the chat-app channel | Deployment decision |
| — | Autoscaling on queue depth; webhook-based chat app; several domains per process | README Roadmap items, not in scope |
