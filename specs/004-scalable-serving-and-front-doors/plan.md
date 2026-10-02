# Implementation Plan: Scalable Serving and Front Doors

**Branch**: `004-scalable-serving-and-front-doors` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-scalable-serving-and-front-doors/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

The system is split into **tiers that scale independently** and **front doors that share one rulebook**.

**Tiers.** A FastAPI service (the *API tier*) accepts `POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel`,
publishes a job on `agent:requests:<domain>` (a Redis Stream) and relays the job's `agent:results:<request_id>`
stream back as Server-Sent Events. It never runs the graph for chat. **Agent workers** (`python -m
app.job_queue.agent_worker`) read that stream as one consumer group per domain. Each worker process serves one
domain for its life (`AGENT_DOMAIN`), runs up to `AGENT_WORKER_MAX_CONCURRENCY` jobs as asyncio tasks bounded by
a semaphore acquired *before* the task is created (so a full worker claims nothing), and shuts down gracefully on
SIGTERM/SIGINT by finishing and acknowledging everything it has claimed. Concurrency works because a turn mostly
waits on the model and tools; the one in-process serialization point, the checkpointer's lock, is replaced by a
semaphore sized to the pool. Capacity is bounded by named settings (Redis connections, pool sizes, concurrency)
and verified by a test that starts real worker processes and by a Locust suite.

**Front doors.** The browser page (`app/api/static/index.html`) speaks only the published stream. The terminal
(`app/channels/chat.py`) and the chat-app channel (`app/channels/telegram.py`) run the runtime in-process through
the same `astream_events_*` entry points the worker uses, differing in identity source (OS user / chat sender) and
approval handling (ask / auto-decline).

The plan records honestly that two reproduced defects (**B5**, **B6**) and eight coverage, signal and
documentation gaps sit around a topology whose *safety* properties — one rulebook for every door, bounded
concurrency, no work lost on a graceful stop — hold where they are asserted.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `fastapi>=0.110,<1` + `uvicorn` (API tier, `StreamingResponse` SSE), `redis>=5,<9`
(Streams, consumer groups, `SET NX EX`, Lua), `limits>=4,<6` (moving-window rate limit over Redis), `httpx`
(Telegram Bot API), `psycopg[binary,pool]` + `langgraph-checkpoint-postgres==2.0.25` (checkpointer pool),
`opentelemetry-sdk` (metrics pushed over OTLP), `langfuse` (tracing), `locust==2.46.4` and `pytest-playwright`
(dev only).

**Storage**: Redis — `agent:requests:<domain>`, `agent:results:<request_id>`, `agent:lock:<thread_id>`,
`agent:cancel:<thread_id>`, `chat:submit_dedup:…`, `<requests stream>:dead`, `telegram:offset:<domain>`, and the
rate limiter's own keys (via `limits`); Postgres — the checkpointer database and `appdata`. The browser holds its
own preferences (`localStorage`: theme, recent tenant/principal values). No new tables.

**Testing**: pytest hermetic tier for the queue and worker (`tests/job_queue/test_queue.py`,
`test_agent_worker.py` — fake Redis), the API handlers and rate limiter (`tests/api/`), and the chat-app channel
(`tests/channels/test_telegram_channel.py`). `integration` tier with real Redis and Postgres:
`tests/integration/test_queue_real_redis.py` and `tests/integration/test_worker_scaling.py` (5 real worker
processes × 50 concurrency, 250 concurrent requests, a concurrent approve round trip, a concurrent delegation run).
`e2e` tier: `tests/live/test_chat_ui.py` (Playwright, real model). **Not tested**: `run()`'s graceful-shutdown path
(A1), the terminal front door at all (A2), the web page's event handling hermetically (A7), a non-default domain's
pool under load (A8).

**Target Platform**: Linux containers — one process per container, scaled by `--scale`; the production compose file
gives the API a 60 s and workers a 90 s stop grace period. The chat-app channel and terminal run as host processes.

**Project Type**: Web service + queue workers + chat-bot channel + CLI.

**Performance Goals**: None asserted as a latency target. Measured: 250 concurrent queued turns complete correctly
across 5 worker processes (concurrency 50 each) against the fake model server.

**Constraints**: `AGENT_WORKER_MAX_CONCURRENCY = 10` (default; semaphore *and* read count); `REDIS_MAX_CONNECTIONS = 300`;
Redis `socket_timeout = None` (blocking reads are bounded by `BLOCK`, 5000 ms); `CHECKPOINTER_POOL_MAX_SIZE = 10`
(and the lock-replacing semaphore's size); appdata pool `max_size = 10` (not configurable); worst case ≈ 20
Postgres connections per graph-running process; `RATE_LIMIT_PER_MINUTE = 30` per tenant; Telegram poll window 30 s,
5 s back-off on a poll error, 4000-character message limit; `CHAT_FIRST_RESPONSE_DEADLINE_SECONDS = 30`;
`REQUEST_TIMEOUT_SECONDS = 60`.

**Scale/Scope**: 4 domains, each with its own pool; 4 front doors; 1 API tier.

**Unknowns**: none — every value is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | Yes — identity enters here | **PASS with the feature-002 caveats** | Identity is stamped at each door and never from message content: HTTP headers (`get_ctx`, 422 if absent), terminal `_LOCAL_CTX`, chat-app `_ctx_for_user`. The chat-app tenant is the shared default and its principal is the *sender* while its thread is the *chat* — in a group the two differ (feature 002 B2; A6 here). The rate limiter keys on the spoofable tenant header (feature 001). |
| II | Mandatory approval (NN) | Yes — doors differ in who approves | **PASS** | Every door reaches the gate through the same entry points. Web/terminal ask a person; the chat app uses `astream_events_turn_unattended` and declines (feature 003). The terminal prompts for *any* pause, not only with `--hitl`. No door has a bypass. |
| III | Fixed, typed tools | No | n/a | |
| IV | Exactly-once side effects (NN) | Yes — at-least-once delivery | **PASS (inherited)** | The worker acks only after a handler ends (feature 003). The chat-app channel is explicitly at-least-once on its read position: a crash between handling and persisting repeats one message — safe because a chat turn's writes are gated and idempotent. |
| V | Bounded, observable failure | **Primary** | **PASS with 2 defects (B5, B6) and 3 advisories** | Bounded: per-worker concurrency, Redis pool, first-event deadline, poll back-off, stop grace period, rate limit. Fails open deliberately: rate limiter (documented). Compensated: submission claim release (feature 003). **Gaps**: B5 — an unguarded per-message call lets one raise end the channel; **A4** — no queue-depth metric, no alert on `WORKER_LOST`/dead-letter, no worker health signal; A6 — a failed chat-app send is logged, not counted. |
| VI | Untrusted content is data | Marginal | **PASS** | The web page escapes HTML and validates link targets before rendering model/retrieved text (`escapeHtml`, `safeHref`). |
| VII | Test discipline | Yes | **PASS with the known gap and A1, A2, A7, A8** | Queue and worker logic is hermetic; the scale claims are proven with real processes at the `integration` tier (self-skips without Docker). Not tested: graceful shutdown (A1), the terminal (A2), the page's behavior (A7), non-default pools under load (A8). The cap that 250 requests exposed (`REDIS_MAX_CONNECTIONS`) is the standing example of why the real tier exists. |
| VIII | Why-first docs, honest gaps | Yes | **PASS with disclosures** | `WORKER_CONCURRENCY.md` records the reasoning and the measured failures; module docstrings explain each setting. **A5**: a settings comment and a worker docstring contradict the code; **A3**: most of these settings have no example-environment entry despite the constitution's configuration rule. B5, B6, A1–A9 are disclosed here, not yet in the README Roadmap. |

**Gate result (pre-research)**: no violation of a NON-NEGOTIABLE principle. Two Principle V defects (B5, B6) —
B6 is also a violation of feature 001's client contract — and the configuration-rule deviation (A3) are open.
They are *defects*, not justified exceptions; the plan proceeds because it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the connection
budget in `data-model.md` §6 made the A3 consequence concrete: an operator cannot discover the settings that
decide capacity from the example environment file. Writing `contracts/chat-app-channel.md` made B5 and B6
visible as contract violations rather than as isolated bugs.

## Project Structure

### Documentation (this feature)

```text
specs/004-scalable-serving-and-front-doors/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the failures behind each
├── data-model.md                  # Phase 1 — processes, Redis keyspace, connection budget, browser state
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B5/B6 reproductions
├── contracts/
│   ├── worker-process.md          # startup order, concurrency, shutdown, settings, signals
│   ├── chat-app-channel.md        # polling, identity mapping, offset semantics, reply rules, failure policy
│   └── front-doors.md             # what each door sends and renders, and where they differ
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── api/
│   ├── main.py                    # lifespan, get_ctx/get_domain, queued SSE relay, chat endpoints
│   ├── rate_limit.py              # TenantRateLimitMiddleware (moving window, fails open)
│   ├── health.py                  # liveness + readiness probes
│   ├── schemas.py
│   └── static/index.html          # the built-in page — speaks only the published stream
├── job_queue/
│   ├── queue.py                   # per-domain streams, results streams, client pool, lock, dedup (feature 003 owns the protocol)
│   └── agent_worker.py            # run(): startup order, semaphore, graceful stop, reclaim loop
├── channels/
│   ├── chat.py                    # terminal front door
│   └── telegram.py                # chat-app front door
└── core/                          # config.py (the settings), telemetry.py, logging_config.py, metrics.py
Dockerfile · docker-compose.yml · docker-compose.prod.yml · docker-compose.loadtest.yml · Caddyfile
Makefile                           # serve, agent-worker[-support|-ops|-sales], telegram[-support|-sales], chat[-hitl], loadtest-*
WORKER_CONCURRENCY.md              # the sizing decision record
loadtest/                          # fake_llm_server.py, locustfile_queued.py
tests/
├── job_queue/ · api/ · channels/ · integration/ · live/test_chat_ui.py
```

**Structure Decision**: One image, several entrypoints (`Dockerfile`), no new modules. The feature is the
*topology* — which process runs what, how many at once, how it starts and stops — expressed in existing modules.

## Complexity Tracking

> Filled because the Constitution Check found two defects and several gaps. Defects are listed without a
> justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B5 (defect, open)** — a turn that raises ends the chat-app channel; the read position is not advanced, so a restart polls the same message again. Reproduced at function level. | Not needed — the loop guards the *poll* but calls `handle_message` bare; the module assumed `astream_events_turn_unattended` always yields an error event instead of raising. | Wrap each message in a guard that logs with a metric, replies with a generic failure message and advances the position; failing test first (a turn that raises must not end `run()` and must persist the position). Bound repeats if the position must not advance. Its own PR. |
| **B6 (defect, open)** — the terminal and the chat app ignore `retry`, concatenating the rejected draft and the retried answer. Reproduced at function level. | Not needed — both clients predate the `retry` event (added for the web page) and were never updated; feature 001's contract makes it an obligation on every client. | Clear the accumulated text on `retry` in `_run_turn` and `_render_stream`; failing test first with `token, retry, token, done`. The terminal can only clear a line it still controls, so it may need to buffer the answer or print a separator — decide in the PR. |
| **A1** — no test of `run()`'s graceful stop. | The stop path was written with the loop and verified by hand. | A hermetic test that publishes a slow job, sets the stop event and asserts the job acknowledges before `run()` returns and the pools close; cheap with the existing fake Redis. |
| **A2** — the terminal has no tests and renders neither sources nor follow-ups. | A demonstration front door. | Test `_render_stream` with a scripted event list; render citations; fixes B6 for the terminal in the same stroke. |
| **A3** — 33 of 61 settings lack an example-environment entry, including every queue and worker tunable. | The settings were added with their reasoning in `config.py` comments and `WORKER_CONCURRENCY.md`. | A test that fails when a `Settings` field is in neither example file (with an allow-list for deliberate omissions) keeps it from regrowing; then add the entries. Violates the constitution's *Configuration* rule. |
| **A4** — no worker/channel health signal; the image probe is wrong for workers; no queue-depth metric; no dead-letter alert. | Health and depth were deferred with the autoscaler (`WORKER_CONCURRENCY.md`). | Disable the image check for worker services in compose; add a stream-length/pending gauge and a dead-letter alert. Decision needed on what "healthy" means for a worker (heartbeat key vs. stream-lag). |
| **A5** — the `checkpointer_pool_max_size` comment is stale against `runtime.py`, the `agent_worker.py` docstring names a `POST /chat/stream` route that no longer exists, and pattern 43 still describes superseded behavior (no redelivery, one flat stream, eager results delete). | The comment predates the semaphore workaround; the route and the redelivery gap were closed after those texts were written. | Correct all three; docs-only (Governance: a conflicting document MUST be corrected). |
| **A6** — a failed chat-app send is only logged; a group chat shares one thread; messages are handled one at a time. | The second and third are documented demo-scope choices. | Count failed sends (`agent_*` counter + alert only if it can hide committed state — it cannot here, so a counter suffices). |
| **A7** — the page's event handling has no hermetic test. | A browser-resident script; the e2e tier covers it with a real model. | A small JS-in-pytest harness (or extracting the handler) would let `retry`/approval be tested without a model; weigh against adding a JS test dependency. |
| **A8** — non-default domains are never exercised under load. | The scaling test and Locust file predate the per-domain pools. | Add an `X-Domain` user class and a worker started with another domain to the scaling test. |
| **A9** — the chat-app channel has no container/service definition. | Added as a host-run demo front door. | A compose service with `restart: unless-stopped` and a stop grace period of at least one poll window. |
