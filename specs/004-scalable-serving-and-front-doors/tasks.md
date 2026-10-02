---

description: "Task list for feature 004 — Scalable Serving and Front Doors (retrospective)"
---

# Tasks: Scalable Serving and Front Doors

**Input**: Design documents from `/specs/004-scalable-serving-and-front-doors/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle V requires every wait and loop to be bounded and observable, and Principle VII requires a
regression test for every bug fix. Both of this feature's defects (B5, B6) were *missed by the existing tests* — one of
those tests (`TestRunPersistsOffsetAcrossPolls`) even relies on a raise in the message loop to stop `run()` — so the open
test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first**
  (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B5** a raising turn ends the chat-app channel ·
  **B6** the terminal and chat app ignore `retry` · **A1** graceful shutdown untested · **A2** terminal untested and
  renders no sources · **A3** most settings lack an example-environment entry · **A4** no worker health signal, wrong
  image probe, no depth metric or dead-letter alert · **A5** stale comments and pattern · **A6** failed chat-app sends
  uncounted · **A7** the page's behavior has no hermetic test · **A8** non-default domains untested under load ·
  **A9** the chat-app channel has no service definition.
- **Neither defect weakens a safety property**: nothing writes without approval on any door. B5 is a liveness defect;
  B6 delivers a rejected draft alongside the corrected answer (and violates feature 001's client contract).
- Tasks needing Docker say `integration`. Paths are repo-relative.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US6 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] Serving tunables in the central settings object — `agent_domain`, `agent_worker_max_concurrency` (10), `redis_max_connections` (300), `rate_limit_per_minute` (30), `chat_submit_dedup_ttl_seconds` (10), `chat_first_response_deadline_seconds` (30), `worker_reclaim_interval_seconds` (60), `agent_worker_reclaim_idle_seconds` (240), `max_auto_reclaim_retries` (1), `checkpointer_pool_max_size` (10), `cors_allowed_origins`, `telegram_bot_token` — in `app/core/config.py` (**example-environment entries are missing for most — A3**) *(FR-026)*
- [x] T002 [P] One image, several roles: API default `CMD`, workers override `command:` in `docker-compose.yml` — in `Dockerfile` (with its image-level `HEALTHCHECK`, see A4) *(FR-019)*
- [x] T003 [P] Per-domain worker services (`agent-worker`, `-support`, `-ops`, `-sales`), the API, and the ingest worker behind the `app` profile in `docker-compose.yml` *(FR-014, FR-019)*
- [x] T004 [P] Production services with `restart: unless-stopped`, CPU/memory caps and stop grace periods (API 60 s, workers 90 s) in `docker-compose.prod.yml` *(FR-019, FR-022)*
- [x] T005 [P] Run targets `serve`, `agent-worker[-support|-ops|-sales]`, `telegram[-support|-sales]`, `chat`, `chat-hitl`, and the `loadtest-*` targets (which drive `docker-compose.loadtest.yml`) in `Makefile` *(FR-019)*
- [x] T006 [P] The sizing decision record — why 1:1 requests:containers is wrong, one process per container, the two concurrency models, the Postgres connection budget — in `WORKER_CONCURRENCY.md` *(FR-017, FR-020)*
- [x] T007 [P] A model stand-in that answers many requests at once (native Ollama serializes to one) in `loadtest/fake_llm_server.py` *(FR-024)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The queue client, the per-domain streams and the process-side runtime every story depends on.

- [x] T008 The Redis client factory — `decode_responses=True`, `socket_timeout=None`, `max_connections=REDIS_MAX_CONNECTIONS` — `get_client` in `app/job_queue/queue.py` *(FR-018)*
- [x] T009 Per-domain streams and results streams — `requests_stream_key`, `results_stream_key`, `ensure_consumer_group` (`id="0"`), `publish_request`/`publish_resume_request`/`publish_cancel_request` — in `app/job_queue/queue.py` (the protocol itself is feature 003) *(FR-014)*
- [x] T010 The durable checkpointer opened on the calling loop, backed by a pool, with its lock replaced by a semaphore sized to the pool (workaround for langgraph#7259) — `_open_checkpointer` in `app/agent/runtime.py` *(FR-017)*
- [x] T011 Domain resolution that fails loudly on an unknown name — `resolve_domain` in `app/domains/registry.py` *(FR-010, FR-021)*
- [x] T012 [P] Request-id binding per task and JSON logging — `bind_request_id`, `configure_logging` in `app/core/logging_config.py`; per-replica OTLP metric push — `configure_telemetry` in `app/core/telemetry.py` *(FR-016, FR-019)*

**Checkpoint**: Foundation ready — a process can resolve a domain, open its stores and talk to the queue.

---

## Phase 3: User Story 1 — The same assistant, from whichever front door a person uses (Priority: P1) 🎯 MVP

**Goal**: One rulebook behind the web page, the HTTP stream, the terminal and the chat app.

**Independent Test**: Ask the same question through each door; a side effect pauses (web/terminal ask, chat app declines).

### Tests for User Story 1

- [x] T013 [P] [US1] The page references the documented event vocabulary and endpoint, sends the identity and domain headers, and starts a fresh conversation when the caller identity changes in `tests/api/test_api.py` (`TestUi` — HTML substring checks; the page's behavior is **not** tested hermetically, A7) *(FR-002, FR-003)*
- [x] T014 [P] [US1] The chat app: thread per chat, principal per sender, sources footer, splitting, a no-text turn still replies, non-text skipped, a failed send swallowed, an error still replies — in `tests/channels/test_telegram_channel.py` (`TestThreadAndCtx`, `TestFormatReply`, `TestSendMessage`, `TestHandleMessage`) *(FR-006, FR-008)*
- [x] T015 [P] [US1] The page end to end with a real model — a tool call, an approve round trip, read-only data, a citation, a skill, a delegation — in `tests/live/test_chat_ui.py` (`e2e`) *(FR-003, FR-004)*

### Implementation for User Story 1

- [x] T016 [P] [US1] The built-in page — event rendering with draft/promote, tool activity, citations, follow-ups, clarification, approve/reject, stop, session switcher, identity/domain selectors, image attach — in `app/api/static/index.html`, served by `GET /` in `app/api/main.py` *(FR-002, FR-003, FR-004)*
- [x] T017 [P] [US1] The terminal: local identity, in-process turn, `Approve? [y/N]` on any pause, `--hitl` — in `app/channels/chat.py` *(FR-005)*
- [x] T018 [P] [US1] The chat app's unattended turn, reply collection and `Sources:` footer — `_run_turn`, `handle_message`, `_format_reply` in `app/channels/telegram.py` *(FR-006)*

### Open follow-ups for User Story 1 (not built) — **B6, A2**

- [ ] T019 [US1] **B6 — write the failing test first**: in `tests/channels/test_telegram_channel.py` add a test that `_run_turn` fed `token "…90 days. "`, `retry`, `token "…30 days."`, `done` returns only `"…30 days."`. Fails today: it returns both, concatenated (reproduced — see quickstart *Scenario B6*) *(FR-007, SC-009)*
- [ ] T020 [US1] **B6 — fix (chat app)**: reset the accumulated text on `retry` in `_run_turn` in `app/channels/telegram.py`; update pattern 42 in `GRAPH_PATTERNS.md` *(FR-007)*
- [ ] T021 [P] [US1] **A2 — write the terminal tests**: new `tests/channels/test_chat_cli.py` covering `_render_stream` (tokens inline, tool lines, `approval_required` returns `True`, `error` in red, `done` newline) and `_async_turn` (prompts on *any* pause, only `y` approves, repeats while the run keeps pausing), plus a failing test that a `retry` discards the rejected draft (B6, terminal) *(FR-005, FR-007)*
- [ ] T022 [US1] **B6/A2 — fix (terminal)**: in `app/channels/chat.py` honor `retry` (buffer the answer until the turn ends, or print a separator — decide in the PR) and render `citations` and `followups`; update `specs/…/contracts/front-doors.md`'s table when done *(FR-007, SC-006)*

**Checkpoint**: US1 is *verified* on every door only after T019–T022.

---

## Phase 4: User Story 2 — A burst of requests is absorbed by a queue, not by a container per request (Priority: P1)

**Goal**: API and workers scale independently; one pool per domain; bounded concurrency with backpressure.

**Independent Test**: Several real workers behind a real server answer far more concurrent requests than any one worker's limit, correctly.

### Tests for User Story 2

- [x] T023 [P] [US2] Concurrency is bounded **and** actually overlaps — in `tests/job_queue/test_agent_worker.py` (`TestConcurrentDispatch`); same-thread jobs exclude each other (`TestSameThreadJobsAreSerialized`) *(FR-015, FR-016)*
- [x] T024 [P] [US2] Publishing, consumer-group creation and delivery against a fake Redis in `tests/job_queue/test_queue.py` (`TestPublishRequest`, `TestEnsureConsumerGroup`) *(FR-014)*
- [x] T025 [P] [US2] The producer/consumer round trip against a real Redis Stack in `tests/integration/test_queue_real_redis.py` (`integration`) *(FR-014)*
- [x] T026 [US2] **The capacity test**: 5 real worker processes × concurrency 50 behind a real HTTP server — 250 concurrent turns, concurrent approve round trips, concurrent delegation — all correct, in `tests/integration/test_worker_scaling.py` (`integration`) *(FR-024, SC-001)*
- [x] T027 [P] [US2] The endpoints publish onto the request's domain stream and relay results in `tests/api/test_api.py` (`TestChatStreamQueued`, `TestChatResume`, `TestChatCancel`) *(FR-001, FR-014)*

### Implementation for User Story 2

- [x] T028 [US2] The worker read loop — semaphore acquired *before* the task, read count equal to the bound, slot released in `finally` — in `app/job_queue/agent_worker.py` (`run`, `_process_with_limit`) *(FR-015, FR-016)*
- [x] T029 [P] [US2] The queued endpoints and SSE relay (`_queued_sse_response`) in `app/api/main.py` *(FR-001)*
- [x] T030 [P] [US2] The Redis connection-pool ceiling found by the 250-way run — `REDIS_MAX_CONNECTIONS` in `app/core/config.py`, applied in `get_client` in `app/job_queue/queue.py` *(FR-018)*
- [x] T031 [P] [US2] The per-tenant rate limit (moving window, fails open, never on cancel/health/reads) in `app/api/rate_limit.py`, tested in `tests/api/test_rate_limit.py` *(FR-027, SC-007)*

### Open follow-ups for User Story 2 (not built) — **A5, A8**

- [ ] T032 [P] [US2] **A5 — correct the stale texts (docs-only, do first)**: the `checkpointer_pool_max_size` comment in `app/core/config.py` (the lock is replaced by a semaphore — see `app/agent/runtime.py`); the `agent_worker.py` module docstring's "direct in-process `POST /chat/stream`" in `app/job_queue/agent_worker.py`; and `GRAPH_PATTERNS.md` pattern 43 (redelivery *is* wired up, streams are per-domain, the results stream is not deleted eagerly). Constitution Governance: a conflicting document MUST be corrected *(FR-017)*
- [ ] T033 [US2] **A8 — exercise a non-default domain's pool**: add an `X-Domain` user class to `loadtest/locustfile_queued.py` and a worker started with another `AGENT_DOMAIN` (plus requests for it) to `tests/integration/test_worker_scaling.py` (`integration`) *(FR-014, FR-025)*

**Checkpoint**: after T033 the per-domain pool claim is *proven* under real concurrency, not only routed in a hermetic test.

---

## Phase 5: User Story 3 — Processes start in a safe order, stop without dropping work, recover from a crash (Priority: P2)

**Goal**: Fail loud on a typo, see the backlog, drain on a stop signal, close every pool.

**Independent Test**: A stop signal during a turn lets it finish and acknowledge; an unknown domain refuses to start.

### Tests for User Story 3

- [x] T034 [P] [US3] The recovery loop starts, survives a failed pass and stops on the event in `tests/job_queue/test_agent_worker.py` (`TestReclaimLoop`) *(FR-021)*
- [x] T035 [P] [US3] The chat app refuses to start without a token and resolves the domain before polling in `tests/channels/test_telegram_channel.py` (`TestRun`) *(FR-010, SC-004)*

### Implementation for User Story 3

- [x] T036 [US3] The startup order (resolve → open stores on this loop → create the group → signals → recovery loop → read) and the graceful stop (read nothing more, drain and acknowledge, close the client and both pools) in `app/job_queue/agent_worker.py` (`run`) *(FR-021, FR-022, SC-002)*
- [x] T037 [P] [US3] The API lifespan — open the checkpointer on its own loop, configure telemetry there, close both pools on shutdown — in `app/api/main.py` (`lifespan`) *(FR-023)*

### Open follow-ups for User Story 3 (not built) — **A1**

- [ ] T038 [US3] **A1 — write the shutdown test**: in `tests/job_queue/test_agent_worker.py` add `TestGracefulShutdown` that runs `agent_worker.run()` against the fake Redis with one slow job in flight, sends SIGTERM to the process once the job has started, and asserts the job's terminal event was published and the entry acknowledged **before** `run()` returns, no further entry was read, and both pool-closing functions were called. Passes today only if the behavior is as documented — if it fails, that is a finding *(FR-022, SC-002)*
- [ ] T039 [P] [US3] **A1 — the same for the chat-app channel**: in `tests/channels/test_telegram_channel.py` assert that a stop signal during a message lets that message finish and persist its position, then closes both pools *(FR-013)*

**Checkpoint**: after T038–T039 FR-013/FR-022 and SC-002 are *asserted*, not only designed.

---

## Phase 6: User Story 4 — The chat-app channel is durable and answers everyone who writes (Priority: P2)

**Goal**: A restart repeats at most one message; a failure while handling one message never silences the rest.

**Independent Test**: Restart the channel mid-stream; send a long grounded question and a photo.

### Tests for User Story 4

- [x] T040 [P] [US4] The position is loaded, persisted after each message and scoped per domain in `tests/channels/test_telegram_channel.py` (`TestOffsetPersistence`, `TestRunPersistsOffsetAcrossPolls`) *(FR-009, SC-003)*

### Implementation for User Story 4

- [x] T041 [US4] The long-poll loop, the durable position (`telegram:offset:<domain>`) persisted after handling, the 5 s back-off and per-message split/typing/skip — `run`, `handle_message`, `_send_message`, `_load_offset`, `_save_offset` in `app/channels/telegram.py` *(FR-008, FR-009, FR-011)*
- [x] T042 [P] [US4] The `telegram:` thread-id namespace shared with the ownership gate — `TELEGRAM_THREAD_PREFIX` in `app/agent/sessions.py` (feature 002) *(FR-006)*

### Open follow-ups for User Story 4 (not built) — **B5, A6, A9**

- [ ] T043 [US4] **B5 — write the failing test first**: in `tests/channels/test_telegram_channel.py` add a test that, with a message whose turn raises, `run()` keeps polling, the user receives a reply, and the position is advanced past that update. Fails today: `run()` raises and the position stays unset (reproduced — quickstart *Scenario B5*) *(FR-012)*
- [ ] T044 [US4] **B5 — fix**: guard each message in `run()` in `app/channels/telegram.py` (`except Exception  # noqa: BLE001 - one bad message must not end the channel`), log `telegram_message_failed`, count it, send a generic failure reply and advance + persist the position; decide in the PR whether a transient failure should instead bound re-handling; update pattern 42 in `GRAPH_PATTERNS.md`. Note: `TestRunPersistsOffsetAcrossPolls::test_persists_the_new_offset_after_each_message_is_handled` stops `run()` by raising from `_save_offset`; it must keep working *(FR-012)*
- [ ] T045 [P] [US4] **A6 — count failed sends**: add a counter in `app/core/metrics.py`, increment it in `_send_message` in `app/channels/telegram.py`, with a test in `tests/channels/test_telegram_channel.py` (`TestSendMessage`); no alert needed (it cannot hide committed state — nothing here writes) *(FR-011)*
- [ ] T046 [P] [US4] **A9 — a service definition for the channel**: a `telegram` service with `restart: unless-stopped` and a `stop_grace_period` of at least the 30 s poll window, in `docker-compose.yml` and `docker-compose.prod.yml` (deployment decision: one service per domain/token) *(FR-013)*

**Checkpoint**: US4 meets FR-012 after T043–T044.

---

## Phase 7: User Story 5 — Capacity limits are explicit, documented and proven with real processes (Priority: P2)

**Goal**: Every scale-bounding number is named, reasoned, and discoverable.

**Independent Test**: Run the scaling test; read the settings reference and the example environment file.

### Tests for User Story 5

- [x] T047 [P] [US5] The Redis client has no socket timeout shorter than a blocking read in `tests/job_queue/test_queue.py` (`TestGetClient`) *(FR-018)*
- [x] T048 [P] [US5] The load suite — plain, cached, long-history, approve/reject, cancel mid-stream, resume with nothing paused, invalid identity, rate limit, upload — in `loadtest/locustfile_queued.py` (`make loadtest-queued`), wired to the fake model by `docker-compose.loadtest.yml` *(FR-025)*

### Implementation for User Story 5

- [x] T049 [P] [US5] The Postgres connection-budget reasoning and the exhaustion symptom in `WORKER_CONCURRENCY.md`; the integration database sized for the stack in `tests/containers.py` (`ensure_postgres`) *(FR-020)*

### Open follow-ups for User Story 5 (not built) — **A3**

- [ ] T050 [US5] **A3 — write the failing test first**: new `tests/core/test_settings_example_env.py` asserting every field of `Settings` appears in `.env.example` or `.env.prod.example`, or in an explicit allow-list that states a reason per omission. Fails today for 33 of 61 fields (incl. `agent_worker_max_concurrency`, `redis_max_connections`, `checkpointer_pool_max_size`, `chat_first_response_deadline_seconds`, the reclaim settings, `request_timeout_seconds`) *(FR-026, SC-008)*
- [ ] T051 [US5] **A3 — fix**: add commented example entries (default and a one-line reason) for the omitted settings to `.env.example` and `.env.prod.example`; allow-list only secrets and settings that must not be tuned; correct feature 003's task T005, whose wording claims entries that do not exist *(FR-026, SC-008)*
- [ ] T052 [P] [US5] **Pin the connection ceiling hermetically**: add a test to `tests/job_queue/test_queue.py` (`TestGetClient`) asserting `max_connections == REDIS_MAX_CONNECTIONS`; today only the integration scaling test guards it *(FR-018)*

**Checkpoint**: after T050–T051 the settings that decide capacity are discoverable from the example environment.

---

## Phase 8: User Story 6 — Operators can tell the tiers are alive and where a burst is going (Priority: P3)

**Goal**: Liveness and readiness for the API; a health signal and depth metric for the rest.

**Independent Test**: With no worker running a request errors within the deadline; readiness names each store.

### Tests for User Story 6

- [x] T053 [P] [US6] Readiness maps each dependency to a boolean and bounds a hung check in `tests/api/test_health.py` *(FR-028, SC-005)*

### Implementation for User Story 6

- [x] T054 [P] [US6] `GET /health` and `GET /health/ready` in `app/api/health.py`; the image-level `HEALTHCHECK` for the API role in `Dockerfile` *(FR-028)*
- [x] T055 [P] [US6] Alert rules `RateLimitRejectionSpike` and `ScrapeTargetDown` in `observability/prometheus/alerts.yml` *(FR-028, FR-027)*

### Open follow-ups for User Story 6 (not built) — **A4, A7**

- [ ] T056 [US6] **A4 — stop the image probe failing the workers**: add `healthcheck: {disable: true}` (or a worker-appropriate check) to every worker service in `docker-compose.yml` and `docker-compose.prod.yml`; verify with `docker compose ps` (`integration`/manual; needs Docker). Decide first what "healthy" means for a worker (a heartbeat key vs. stream lag) *(FR-028)*
- [ ] T057 [P] [US6] **A4 — a depth signal and a dead-letter alert**: a gauge of the stream's length and pending entries, published from the worker's recovery loop in `app/job_queue/agent_worker.py` and defined in `app/core/metrics.py`; alert rules for sustained backlog and for `agent_worker_job_reclaimed_total{outcome="dead_lettered"}` in `observability/prometheus/alerts.yml`; a hermetic test of the gauge in `tests/job_queue/test_agent_worker.py` *(FR-028)*
- [ ] T058 [P] [US6] **A7 — decide and, if adopted, build a hermetic harness for the page's event handling** (`retry`, draft/promote, approval buttons) — either extract the handler into a testable module or add a small JS test runner; test file `tests/api/test_page_events.py`. Weigh the added dependency; record the decision in research.md *(FR-003)*

**Checkpoint**: after T056–T057 an operator can see that a worker is alive and that work is stuck.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T059 [P] **Disclose every open gap in the project docs now (docs-only)** — Principle VIII: add one entry each for **B5**, **B6**, **A1**–**A9** to `GRAPH_PATTERNS.md` *Extending Further* and a short list to the README *Roadmap*, each stating how it was established (reproduced vs. read) and that no unreviewed write results from any of them. Land this before any fix
- [ ] T060 Re-run `specs/004-scalable-serving-and-front-doors/quickstart.md` Tiers 1–2 and both scenarios after the fixes; delete each resolved row from plan.md *Complexity Tracking* and each resolved gap from spec.md *Known gaps*
- [ ] T061 [P] After B5/B6 land, update pattern 42 in `GRAPH_PATTERNS.md` (it states "a message is retried … if the process dies mid-handling" but not what happens when the *turn* raises)

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup → Foundational → stories.** Foundational blocks every story.
- **US1** (P1) needs only Phase 2; its open tasks (B6, A2) are independent of the other stories.
- **US2** (P1) needs Phase 2; the scaling test (T026) is the end-to-end proof for US2, US3 and US5.
- **US3** (P2) needs US2's worker loop; **US4** (P2) needs US1's chat-app turn (T018).
- **US5** (P2) is independent of US1–US4 at the code level.
- **US6** (P3) needs the worker loop (T028) for the depth gauge.
- **Polish** last — except **T059** and **T032**, which land first.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T059, T032 | `GRAPH_PATTERNS.md`, README, two comments | docs-only; do first |
| 2 | T043–T044 (B5) | `telegram.py`, one test file | test-first; needs the advance-or-bound decision |
| 3 | T019–T020 (B6, chat app) | `telegram.py`, one test file | test-first; tiny |
| 4 | T021–T022 (A2, B6 terminal) | `chat.py`, new test file | decision: buffer vs. separator |
| 5 | T038–T039 (A1) | two test files | tests only; may surface a defect |
| 6 | T050–T051 (A3) | new test, two example files | large but mechanical; split the entries if over the line budget |
| 7 | T052 | `test_queue.py` | tests only |
| 8 | T045 (A6) | `telegram.py`, `metrics.py`, test | small |
| 9 | T056–T057 (A4) | compose files, worker, `metrics.py`, `alerts.yml` | needs a "healthy" definition; Docker |
| 10 | T033 (A8) | locustfile, scaling test | `integration`; Docker |
| 11 | T046 (A9) | compose files | deployment decision |
| — | T058 (A7) | decision | weigh a JS test dependency |

PRs 2–5 and 7–8 are mutually independent; run them in parallel.

### Parallel opportunities

- Setup T001–T007 and Foundational T012 are [P].
- After Phase 2, US1/US2/US5 in parallel; within a story every test task is [P].

## Parallel Example: User Story 4

```bash
# Tests together (same file, different classes — write sequentially, run together):
Task: "T040 Position tests in tests/channels/test_telegram_channel.py"
Task: "T043 B5 failing test in tests/channels/test_telegram_channel.py"
# Implementation:
Task: "T044 Guard each message in app/channels/telegram.py"
Task: "T045 Count failed sends in app/core/metrics.py"
```

## Implementation Strategy

### As-built order (what happened)

The chat-app channel came first (2026-08-28, in-process, in the original package reorganization) → the three example
domains (08-30) → the in-process `/chat` and `/chat/stream` endpoints were removed in favor of the queued path
(09-05) → worker concurrency made configurable and the **Redis pool ceiling found at 250-way** (09-03/04) → ingestion
adopted the same concurrency model (09-11) → async migration (09-18) → a concurrency-races audit (09-22) → crash
recovery and safe retry (09-25/26, feature 003) → the test Postgres sized for the stack it hosts (10-02). Per-domain
streams (pattern 49) turned one API into a front for every domain's pool. The two defects (B5, B6) sit at the *edges*
of the chat-app and terminal clients — where a failure path or a newer event type was never exercised.

### Closing the open follow-ups (what to do next)

1. **PR 1 (docs)** now — pattern 43 is *wrong* about redelivery, and the disclosures make the open gaps visible.
2. **PRs 2–3 (B5, B6 chat app)** — the only defects that reach a real user; both tiny and test-first.
3. **PRs 4–5, 7–8** — terminal, shutdown, connection ceiling, send counter.
4. **PR 6 (A3)** — make capacity discoverable.
5. **PRs 9–11** — operability: probes, depth, a domain pool under load, a service for the channel.
6. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 (T001–T033 minus the open follow-ups) is the minimum that gives one rulebook behind every door and a queue-backed
tier that scales; **US3** makes deploys safe. Neither B5 nor B6 weakens a safety property, so the system is *safe* to run as it
stands — but not **fully correct**: B5 can silence a chat-app channel, and B6 can show a chat user a rejected answer.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (195 passed) was re-run on 2026-10-02, plus the two
  reproduction scenarios.
- Tier 2/3 and the capacity claims are **not** verified by this batch.
- Features 001 (the turn and its event contract), 002 (identity and ownership) and 003 (the job protocol) own behavior
  this feature relies on.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks.
