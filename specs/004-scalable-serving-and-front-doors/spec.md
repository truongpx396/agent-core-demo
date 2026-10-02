# Feature Specification: Scalable Serving and Front Doors

**Feature Branch**: `004-scalable-serving-and-front-doors`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with two reproduced defects, see *Known gaps* B5 and B6

**Input**: User description: "Scalable serving and front doors (retrospective spec of the as-built system): the assistant is reachable from a built-in web page, an HTTP API, a terminal and a chat app; the web/HTTP path never runs the assistant inside the request — it hands each turn to a queue that independently scalable worker processes drain, one pool per domain, each bounded in how many turns it runs at once; processes start in a safe order, stop gracefully, and recover from a crash; the chat-app channel remembers where it stopped; and capacity is demonstrated with real processes rather than assumed."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code,
> the constitution (*Architecture & Technology Constraints → Queueing*; Principle V), `GRAPH_PATTERNS.md`
> patterns 29, 42, 43 and 49, and `WORKER_CONCURRENCY.md`. It describes what the system does today.
> **Boundaries.** What a turn *does* is feature 001; who may reach which conversation is feature 002; the
> job protocol — delivery, the per-conversation lock, crash classification, dead letters — is feature 003
> (`contracts/queue-job-protocol.md`) and is *used* here, not re-specified. This feature is the **serving
> topology**: the front doors, the worker process model, scaling and sizing, lifecycle, and how capacity
> is verified.
> Two defects were found by *reproducing* behavior with a hermetic harness while writing it (B5, B6);
> nine further gaps were found by reading the code, tests and deployment files. All are in *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The same assistant, from whichever front door a person uses (Priority: P1)

A person can ask a question from the built-in web page, from any program that speaks the HTTP stream,
from a terminal, or from a chat app. It is the same assistant behind each door: the same answers, the
same citations, the same pause-for-approval rule for anything with a side effect. What differs is only
how each door treats an approval request, because the audiences differ: the web page and the terminal
have a person in front of them and *ask*; the chat app has nobody to ask and *declines*. A door never
invents a private route around the shared rules.

**Why this priority**: A second interface that quietly relaxed a rule (skipped the approval gate,
accepted a different identity, reached around the queue) would be an unreviewed back door. Keeping one
behavior behind every entrance is what lets the rest of the guarantees (features 001–003) be stated once.

**Independent Test**: Ask the same grounded question through the web page, the HTTP stream and the
terminal; confirm the same answer text (and the same sources on the doors that render them). Ask each to do something with a side effect; confirm
the web page and terminal show an approve/reject choice and the chat app replies that it needs a person's
approval and does nothing.

**Acceptance Scenarios**:

1. **Given** the built-in web page, **When** a person sends a message, **Then** the page uses only the
   published HTTP stream (no private endpoint), sends its identity and domain choices as the documented
   headers, and renders the stream incrementally — draft text, tool activity, sources, suggested
   follow-ups, a clarifying question, or an approve/reject choice.
2. **Given** a person changes the tenant, the principal or the domain on the web page mid-conversation,
   **When** they next send, **Then** a fresh conversation starts rather than continuing the old one under
   the new identity.
3. **Given** a conversation paused for approval, **When** the person reopens the page or switches back to
   it, **Then** the pending choice is shown again and a stop button cancels the run.
4. **Given** the terminal front door, **When** the assistant pauses for approval — for *any* side effect,
   whether or not the optional "ask about every tool call" flag is set — **Then** the person is asked
   `Approve? [y/N]` and anything other than `y` is a rejection.
5. **Given** the chat-app front door, **When** the assistant wants a side effect, **Then** it is declined,
   the user is told it needs a person's approval, and nothing is written.
6. **Given** a front door that renders a streamed answer, **When** the quality gate rejects a draft and
   asks the client to discard it, **Then** the rejected text is not shown to the person as part of the
   answer. **As built this holds for the web page only — the terminal and the chat app concatenate the
   rejected draft with the retried answer (see B6).**

---

### User Story 2 - A burst of requests is absorbed by a queue, not by a container per request (Priority: P1)

Fifty simultaneous questions do not need fifty running programs. The HTTP tier accepts each request and
places the work on a queue; a pool of worker processes drains it. The two tiers scale separately — more
web-serving processes for more simultaneous connections, more workers for more simultaneous turns — and
each domain (the default assistant, support, ops, sales) has its **own** pool, so one domain's backlog
never delays another's. Each worker runs several turns at once because a turn mostly *waits* (on the
model, on a tool), and stops taking new work when it is full so unclaimed work stays on the queue for
a worker that has room.

**Why this priority**: Without this the system serves one conversation at a time or needs capacity sized to the
peak. The split also contains the blast radius of a slow model: the web tier keeps answering health and
read requests while workers are saturated.

**Independent Test**: Start several worker processes and the HTTP service against a model stand-in that
can answer many requests at once. Fire many more concurrent requests than any one worker's limit. Confirm
every one completes with a correct answer, that no worker ever ran more turns than its limit, and that
adding workers raises the number served at once.

**Acceptance Scenarios**:

1. **Given** a request, **When** it reaches the HTTP service, **Then** the service never runs the
   assistant itself; it publishes a job on the stream for the request's domain and relays what a worker
   publishes back.
2. **Given** several workers for one domain, **When** jobs arrive, **Then** each job goes to exactly one
   worker (consumer-group delivery) and the load is split between them.
3. **Given** requests for different domains, **When** they arrive at one HTTP service, **Then** each lands
   on its own domain's stream and is served only by a worker started for that domain.
4. **Given** a worker already running its configured maximum, **When** more jobs are waiting, **Then** it
   claims no more — they stay pending on the stream for another worker.
5. **Given** a model backend that serializes generations, **When** many turns are in flight, **Then**
   throughput is bounded by that backend, not by the worker count — worker concurrency is tuned to the
   backend's real concurrency, not to the request rate.
6. **Given** a turn that is waiting on a slow model or tool, **When** other jobs arrive, **Then** they run
   in the meantime on the same process (no head-of-line blocking).
7. **Given** scaling the number of workers or web processes, **When** replicas start, **Then** no
   monitoring configuration needs to change (each replica pushes its own metrics to a shared collector).

---

### User Story 3 - Processes start in a safe order, stop without dropping work, and recover from a crash (Priority: P2)

A worker refuses to start on a misspelled domain name rather than serving the wrong one. It opens its
conversation store on its own event loop before taking work, makes sure its queue group exists (and sees
work that arrived before it started), and then reads. When asked to stop it finishes every job it has
already claimed, acknowledges them, closes its connections, and exits — so a routine redeploy loses
nothing. If it is *killed* instead, another worker's recovery sweep (feature 003) picks up what it
abandoned.

**Why this priority**: Deploys and restarts are the most frequent "failure" a queue worker sees. A stop
that abandoned in-flight turns would turn every release into a burst of lost-worker errors.

**Independent Test**: Start a worker with a turn in flight, send it a stop signal, and confirm the turn
finishes and is acknowledged before the process exits; start a worker with an unknown domain and confirm
it refuses to start with the list of valid names.

**Acceptance Scenarios**:

1. **Given** an unknown or misspelled domain, **When** a worker (or the chat-app channel) starts, **Then**
   it fails immediately, naming the valid domains.
2. **Given** work already on the stream before a worker starts, **When** it creates its group, **Then** it
   sees that backlog (the group starts from the beginning).
3. **Given** a stop signal while jobs are in flight, **When** the worker shuts down, **Then** it reads no
   further entries, lets every claimed job run to completion and acknowledge, stops its recovery loop,
   and closes its queue connection and both database pools.
4. **Given** a worker killed without a graceful stop, **When** another worker's recovery sweep runs,
   **Then** the abandoned job is handled per feature 003.
5. **Given** the HTTP service starts, **When** it comes up, **Then** it opens the conversation store on
   its own event loop and configures telemetry there (not at import time), and it closes its pools on
   shutdown.

---

### User Story 4 - The chat-app channel is durable and answers everyone who writes (Priority: P2)

A person messages the bot and gets a reply, including a "needs a person's approval" reply when they asked
for a side effect and a polite "couldn't put a reply together" when a turn produced nothing. Each chat is
one durable conversation that survives a restart; each person is their own principal inside one shared
tenant. The channel remembers how far it has read, and only moves that mark *after* a message is handled,
so a restart repeats at most the one message in flight (a duplicate reply) and never silently drops one.
A long answer is split rather than truncated. A network error while polling or sending does not stop the
channel. It refuses to start without a bot token and needs no public address.

**Why this priority**: It is the one front door that reaches the public internet and has no human
approver, so its failure modes — a repeated flood after a restart, a silent non-reply, a stalled loop —
are the ones a real user feels.

**Independent Test**: Send a message, restart the channel mid-stream, and confirm the earlier message is
not answered again (beyond one in-flight message). Send a long grounded question and confirm the reply
arrives in parts with its sources. Send a photo and confirm it is ignored without error.

**Acceptance Scenarios**:

1. **Given** no bot token, **When** the channel starts, **Then** it refuses with an explanatory error.
2. **Given** a text message, **When** it is handled, **Then** the chat shows a typing indicator, the turn
   runs unattended, and the reply carries the answer plus a "Sources" list if there are citations.
3. **Given** a reply longer than the per-message limit, **When** it is sent, **Then** it is split into
   consecutive messages, not truncated.
4. **Given** a message that is not text (a photo, sticker, voice note), **When** it arrives, **Then** it
   is skipped without error and its position is still advanced.
5. **Given** the channel restarts, **When** it resumes polling, **Then** it continues from the persisted
   position for its domain, not from zero.
6. **Given** a poll or a send fails, **When** the error is caught, **Then** the loop continues (a poll
   failure waits briefly and retries; a failed send is logged and skipped).
7. **Given** two users in different private chats, **When** they write, **Then** each has their own
   conversation and their own principal (and so their own memories).
8. **Given** a message whose turn raises instead of finishing, **When** it is handled, **Then** the
   channel *should* log it, tell the user, advance past it, and keep serving. **As built the whole
   channel stops, and because the position was not advanced the same message is fetched again on
   restart (see B5).**

---

### User Story 5 - Capacity limits are explicit, documented and proven with real processes (Priority: P2)

Every number that bounds scale is a named setting with a stated reason: how many turns a worker runs at
once, how many queue connections the process may hold (each open stream holds one for the whole turn),
how big the conversation-store pool is, how many database connections the whole fleet can open. The
reasoning behind them is written down — including the failure each one prevents and what exhaustion
looks like. A test starts real worker processes and a real HTTP server and fires hundreds of concurrent
requests; a load-test suite covers every queued endpoint.

**Why this priority**: The first real load test of this system found that 250 concurrent requests failed
~83% of the time on a connection ceiling nobody had configured. Capacity that is only reasoned about is
not capacity.

**Independent Test**: Run the scaling test (5 worker processes × 50 turns each, 250 concurrent requests)
and confirm every request returns the correct answer. Read the settings reference and confirm each
limit has a default, a reason, and (per the project rule) an example-environment entry.

**Acceptance Scenarios**:

1. **Given** hundreds of concurrent streams, **When** each holds a connection for its whole turn, **Then**
   the queue client's pool is large enough that none fails for lack of a connection, and its blocking
   reads are not cut short by a client-side timeout.
2. **Given** the fleet of processes that run the assistant, **When** their connection pools are summed,
   **Then** the documented worst case (about 20 connections per process) is stated against the
   database's connection limit, and exhaustion is described by its symptom (an answerless 200).
3. **Given** the load-test suite, **When** it runs against the model stand-in, **Then** it exercises plain, cached
   and long-history turns, approve and reject round trips, cancel mid-stream, a resume with nothing paused, an
   invalid identity, rate limiting and upload. *(It never selects a non-default domain — A8.)*
4. **Given** a setting that bounds scale, **When** it is added or changed, **Then** it lives in the central
   settings object and has an example-environment entry. *(Not met for most of these — see A3.)*

---

### User Story 6 - Operators can tell the tiers are alive and where a burst is going (Priority: P3)

The HTTP service answers a liveness probe and a readiness probe that checks the stores it needs. Every
replica pushes metrics and logs correlated by request, so a burst is visible as a rate, a latency and a
rejection count, and a per-tenant rate limit protects the queue from one noisy tenant. Alerts cover rate-limit
spikes and a vanished scrape target.

**Why this priority**: It is last because it concerns observing the system rather than serving a person,
and because its gaps (no worker health probe, no queue-depth signal, no alert on a dead-lettered job)
are the honest edge of what is built.

**Independent Test**: Stop all workers for a domain and confirm a request ends with a clear "is a worker
running?" error within the deadline rather than hanging; confirm readiness reports each store.

**Acceptance Scenarios**:

1. **Given** no worker is running for a domain, **When** a request arrives, **Then** it ends with an error
   naming the cause within the first-response deadline (feature 003).
2. **Given** a tenant exceeding its per-minute budget on the turn-creating endpoints, **When** it sends
   more, **Then** it receives a 429 and a counter increments; cancel and read endpoints are never limited.
3. **Given** a worker is wedged but alive, **When** an operator looks for a health signal, **Then**
   *(intended)* one exists. **As built there is none of its own for workers or the chat channel — see A4.**

---

### Edge Cases

- A browser tab closed mid-turn leaves the worker running that turn to completion; its results stream
  expires by TTL. The tab's own state (identity selections, theme) is stored only in that browser.
- A client that disconnects and retries within the de-duplication window shares the first attempt's turn
  (feature 003); after the window it starts a new turn.
- A web page left open across a deploy can resume a paused turn only if the checkpoint is still
  compatible (feature 003).
- Two chat-app messages for the same chat are handled one after the other — a slow turn delays every other
  chat, because the channel handles messages sequentially by design.
- The long-poll window means a stop signal to the chat-app channel takes up to the poll window to be
  noticed; a message already being handled is never interrupted.
- A model backend that serializes generations makes extra worker concurrency invisible: latency, not
  throughput, grows.
- The chat-app channel and a worker for the same domain are different processes and do not share a lock;
  they share only the conversation store.

## Requirements *(mandatory)*

### Functional Requirements

**Front doors**

- **FR-001**: The web/HTTP path MUST hand each turn to the queue and MUST NOT run the assistant inside the
  request; the terminal and chat-app front doors run the same runtime in-process and MUST use the same
  turn entry points as the worker (so one set of rules applies everywhere).
- **FR-002**: The built-in web page MUST speak only the published HTTP stream and the documented endpoints,
  send the identity and domain headers from user-editable selectors, and start a fresh conversation when
  the tenant, principal or domain changes.
- **FR-003**: The web page MUST render the documented event vocabulary incrementally: draft text that is
  promoted only when the answer is confirmed, a discard on `retry`, tool activity, sources, follow-ups, a
  clarifying question, an approve/reject choice, and a stop control.
- **FR-004**: The web page MUST make a paused turn actionable (approve/reject via the resume endpoint, stop via
  the cancel endpoint) and MUST re-show a pending approval when a session is reopened.
- **FR-005**: The terminal front door MUST run under a local identity (the default tenant and a principal
  derived from the operating-system user) and MUST prompt for approval whenever the run pauses; an optional
  flag additionally gates *every* tool call.
- **FR-006**: The chat-app front door MUST run turns unattended (declining any approval), MUST map each chat to
  one durable conversation and each sender to a principal in the default tenant, MUST always send a
  non-empty reply, and MUST append a "Sources" list when the answer has citations.
- **FR-007**: A front door that renders a streamed answer MUST discard what it has rendered when a `retry`
  event arrives. *(Met by the web page; **not met** by the terminal or the chat app — B6.)*
- **FR-008**: The chat-app front door MUST split a reply longer than its per-message limit rather than
  truncate it, MUST show a typing indicator (best-effort), and MUST skip non-text messages while still
  advancing past them.

**Chat-app channel durability**

- **FR-009**: The channel MUST persist how far it has read, per domain, in a shared store, and MUST advance and
  persist that position only **after** the message is handled (at-least-once, never at-most-once).
- **FR-010**: The channel MUST refuse to start without its token or with an unknown domain, and MUST open the
  conversation store on its own event loop before polling.
- **FR-011**: A polling failure MUST NOT end the loop (wait, then retry) and a send failure MUST NOT end the
  loop (log and skip).
- **FR-012**: A message whose turn raises MUST NOT end the channel and MUST NOT cause the same message to be
  fetched again indefinitely. *(Not met — B5.)*
- **FR-013**: On a stop signal the channel MUST stop polling, MUST NOT interrupt a message being handled,
  and MUST close both database pools before exit.

**Scale-out**

- **FR-014**: Jobs MUST be published to a stream for the request's domain and consumed as one competing-consumer group,
  so that each job is delivered to one worker and N workers split a domain's load; a worker process MUST
  serve exactly one domain for its whole life, fixed at startup.
- **FR-015**: A worker MUST bound the number of turns it runs at once with a counting limit acquired **before** a
  task is created, and MUST read no more entries than that bound, so a full worker claims nothing and
  leaves the rest pending.
- **FR-016**: A turn MUST hold no in-process state that a concurrent sibling could corrupt: conversation state
  lives in the store, per-call resources are pooled, and the request id is carried per task.
- **FR-017**: The conversation store's connection pool MUST be large enough for the concurrency it serves and
  its lock MUST NOT serialize all checkpoint I/O to one operation at a time (a documented workaround for an
  upstream defect, to be removed when the fix ships).
- **FR-018**: The queue client MUST allow enough pooled connections for every concurrently held stream, MUST
  have no client-side timeout shorter than a blocking read's own wait, and MUST fail fast on an unreachable server.
- **FR-019**: Each process MUST be one role per container, scaled by replica count; each replica MUST push its
  own metrics so replicas can be added without changing monitoring configuration.
- **FR-020**: The worst-case database connections of the whole fleet MUST be documented against the database's
  limit, together with the symptom of exhausting it.

**Lifecycle**

- **FR-021**: A worker MUST start in this order: resolve its domain (failing loudly), open the conversation
  store on its own loop, create its consumer group (from the beginning of the stream), install signal
  handlers, start its recovery loop, then read.
- **FR-022**: On a stop signal a worker MUST stop reading, finish and acknowledge every claimed job, stop its
  recovery loop, close its queue client, and close both database pools. A production deployment MUST give the
  process a stop grace period longer than a turn can run (the shipped production compose gives workers 90 s and
  the HTTP service 60 s against a 60 s turn limit), or the graceful path is cut short.
- **FR-023**: The HTTP service MUST open the conversation store on its own loop at startup, configure telemetry
  there rather than at import, and close its pools at shutdown.

**Verification and operation**

- **FR-024**: Capacity claims MUST be demonstrated by a test that starts real worker processes and a real HTTP
  server and fires concurrent requests against a model stand-in that supports real concurrency.
- **FR-025**: A load-test suite MUST cover every queued endpoint and the failure paths operators care about.
- **FR-026**: Every setting that bounds scale or timing MUST be in the central settings object with an
  example-environment entry. *(Met for the settings object; **not met** for most example-environment
  entries — A3.)*
- **FR-027**: The turn-creating endpoints MUST be rate-limited per tenant over a moving window, the limiter MUST
  fail open if its store is down, and cancel, health and read endpoints MUST never be limited. (Detail:
  feature 001's `chat-turn-http.md`.)
- **FR-028**: The HTTP service MUST expose liveness and readiness; each worker and channel process SHOULD expose
  a health signal of its own. *(None exists, and the inherited image probe cannot pass for them — A4.)*

### Key Entities *(include if feature involves data)*

- **Front Door**: One of the web page, the HTTP stream, the terminal, the chat app. Differs in identity
  source and in who approves.
- **Domain Pool**: The set of worker processes started for one domain, consuming that domain's stream as one
  consumer group.
- **Worker Process**: One OS process: a domain, a consumer name, a concurrency bound, a set of in-flight
  tasks, a stop signal, a recovery loop.
- **Requests Stream / Results Stream**: Per-domain input and per-request output queues (feature 003).
- **Read Position**: The chat-app channel's persisted "next update" marker, one per domain.
- **Conversation Thread (chat app)**: One per chat; its principal is the *sender*, which differs from the
  thread's owner in a group chat.
- **Connection Budget**: The sum of every pool across every process, compared with the database's limit.
- **Browser State**: Per-browser preferences and recently used identity values; never sent anywhere.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 250 concurrent turns across 5 worker processes of concurrency 50 each all return correct answers
  against the model stand-in (the scaling test) — and the same holds for a concurrent approval round trip and a
  concurrent delegation run.
- **SC-002**: Zero claimed jobs are abandoned by a graceful stop (every claimed job acknowledges before exit).
  *(Designed; **not asserted by any test** — A1.)*
- **SC-003**: After a chat-app restart, at most one message (the one in flight) is handled twice; none is lost.
- **SC-004**: A misspelled domain stops a worker or channel at startup, before it takes any work, naming the
  valid domains.
- **SC-005**: With no worker running for a domain, a request ends with an error within the first-response
  deadline (30 s default) rather than hanging.
- **SC-006**: The same question through the web path and the terminal yields the same answer text. *(Holds for
  the answer; the terminal renders no sources or follow-ups at all — A2.)*
- **SC-007**: A tenant over its budget receives a 429 on turn-creating endpoints and never on cancel, health or
  reads; the rejection is counted.
- **SC-008**: Every scale-bounding setting has a central definition, a stated reason, and an example-environment
  entry. *(Not met: 33 of 61 settings, including every queue and worker tunable, lack one — A3.)*
- **SC-009**: A rejected draft is never shown as part of an answer on any front door. *(Web page only — B6.)*

## Assumptions & Known Gaps

**Assumptions**

- A real deployment puts an authenticating gateway in front of the HTTP service (feature 002); the identity
  headers are trusted-layer values, not authentication.
- The model backend's own concurrency, not the worker count, is usually the ceiling; worker concurrency is a
  tuning knob, not a derived value.
- The default web page and terminal are for development and demonstration; the chat-app channel is the only
  front door that necessarily reaches the public internet.
- One worker process serves one domain; "several domains from one process" is a Roadmap item, not built.

**Out of scope for this feature**

- The turn pipeline (001), identity and ownership (002), approvals and the job protocol (003).
- The ingestion worker's own behavior — it shares the concurrency *model* described here but is feature 006.
- The tool-server (MCP) front door — feature 009.
- Orchestrated crash-restart and autoscaling on queue depth (README Roadmap).
- A webhook-based chat-app deployment and other chat apps (README Roadmap).

**Known gaps (disclosed, with how each was established)**

- **Bug B5 — one message whose turn raises ends the chat-app channel, and the same message is fetched again
  on restart (reproduced at function level).** The poll loop catches a *polling* error but handles each
  message with no guard: a turn that raises (rather than yielding an error event) propagates out of the
  loop. Reproduced with a fake HTTP client serving one message and a turn that raises: the loop ends with the
  exception and the persisted position is still unset, so a restart polls the same message again. Whether a
  *real* code path raises deterministically for one message was not established — what is established is that
  any raise before the turn's own error handling (a store outage mid-turn, an unexpected exception) takes down
  every chat served by the process, and that a deterministic one would crash-loop it. Not fixed here.
- **Bug B6 — the terminal and the chat app do not honor the `retry` event (reproduced at function level).**
  Feature 001's contract requires every client to discard what it has rendered when `retry` arrives. The web
  page does; the chat-app channel and the terminal ignore it. Reproduced by feeding `token "…90 days. "`,
  `retry`, `token "…30 days."`, `done` to each: both produced `"…90 days. …30 days."`. The rejected draft is,
  by construction, an answer the quality gate judged bad — so a chat user can be sent a wrong answer followed
  by the corrected one. Not fixed here.
- **A1 — graceful shutdown is not tested.** `run()`'s stop path (read nothing more, finish and acknowledge
  claimed jobs, close pools) has no test; the worker tests drive `process_request` and the recovery loop
  directly, and the channel tests stop `run()` with a marker exception. *(Read from the tests.)*
- **A2 — the terminal front door has no tests and renders neither sources nor follow-ups.** No test imports
  `app/channels/chat.py`. It prints tokens and tool activity only; `citations`, `followups`, `system_note` and
  `retry` are ignored. *(Read from the code and by grep.)*
- **A3 — most scale-bounding settings have no example-environment entry.** The project rule is "tune a limit
  through `Settings` and add it to `.env.example`". Of 61 settings, 33 appear in neither example file,
  including `agent_worker_max_concurrency`, `ingest_worker_max_concurrency`, `redis_max_connections`,
  `checkpointer_pool_max_size`, `chat_submit_dedup_ttl_seconds`, `chat_first_response_deadline_seconds`,
  `worker_reclaim_interval_seconds`, `agent_worker_reclaim_idle_seconds`, `max_auto_reclaim_retries` and
  `request_timeout_seconds` (`rate_limit_per_minute` is in the production example only). Feature 003's task
  T005 claimed these entries exist; that is corrected in the reconciliation. *(Established by comparing the
  settings fields with both example files.)*
- **A4 — workers and the chat channel have no health signal of their own, the image-level probe is wrong for
  them, and there is no queue-depth or lag metric.** The `Dockerfile` defines one `HEALTHCHECK` (the API's
  `/health/ready` on port 8000). Docker applies an image's health check to every container started from it unless
  the service overrides it; the worker services use the same image, never open that port, and neither compose
  file overrides the check — so, by reading, `docker ps` would show them as unhealthy forever (the Dockerfile's own
  comment says the check is "meaningful for this default role only", which is true of intent, not of behavior).
  *Not run: no Docker was available.* Separately, there is no gauge for stream length or pending entries, and no
  alert on `WORKER_LOST` or on `agent_worker_job_reclaimed_total{outcome="dead_lettered"}`;
  `WORKER_CONCURRENCY.md` itself names autoscaling on queue depth as unbuilt. *(Read from the `Dockerfile`,
  compose files, `metrics.py` and `alerts.yml`.)*
- **A5 — comments and a pattern contradict the code.** (a) The comment on `checkpointer_pool_max_size` says only one turn's
  checkpoint I/O is ever in flight per process regardless of pool size; `app/agent/runtime.py` replaces that lock
  with a semaphore sized to the pool (the workaround for langgraph#7259) — the code is the intent. *(The saver's
  lock was confirmed against the installed library.)* (b) The `agent_worker.py` module docstring calls the queue
  "an opt-in path alongside the direct in-process `POST /chat/stream`"; there is no such route today — the queued
  endpoints are the only HTTP chat path (README, the locustfile, and `grep` of `main.py` agree). (c) `GRAPH_PATTERNS.md`
  pattern 43 still says `XAUTOCLAIM` redelivery "is NOT wired up", describes one flat `agent:requests` stream (pattern 49
  made it per-domain) and an explicit `DEL` of the results stream on a normal finish (removed because a de-duplicated
  second reader needs it). *(Read.)*
- **A6 — the chat-app channel shares one conversation across every user in a group chat, handles messages
  strictly one at a time, and counts a failed send only in a log line.** The first two are stated design
  choices in the module docstring (feature 002's B2 notes the group-chat consequence); the third means a user
  can receive no reply, with the position advanced, and nothing countable. *(Read from the code.)*
- **A7 — the web page's behavior is tested only by HTML substring checks and a live end-to-end tier.** There is
  no hermetic test of its event handling (draft/promote, `retry`, approval buttons). *(Read from the tests.)*
- **A8 — the scaling test and the load test only exercise the default domain's pool.** Neither sets `X-Domain` or
  starts a worker with another `AGENT_DOMAIN`; routing a request onto a non-default domain's stream is covered
  only by a hermetic API test. A per-domain pool under real concurrency is unproven. *(Read from the tests and the
  locustfile.)*
- **A9 — the chat-app channel has no container or service definition.** Neither compose file defines it; it runs
  only as a host process (`make telegram`, `telegram-support`, `telegram-sales`), with no restart policy, no
  stop grace period and no resource limits of its own — while the API and workers have all three in the
  production compose file. Combined with B5, a crashed channel stays down. *(Read from the compose files and the
  Makefile.)*
