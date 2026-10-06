# Feature Specification: Observability and Cost Governance

**Feature Branch**: `008-observability-and-cost-governance`

**Created**: 2026-10-03

**Status**: Implemented (retrospective) — with six reproduced or established defects, see *Known gaps* B17–B22

**Input**: User description: "Observability and cost governance (retrospective spec of the as-built system): every process reports what it is doing to one place an operator can watch; logs and metrics describe what happened without recording what anyone said, and the one place content is kept — an optional trace of each turn — is separate; every completed turn's token use is written to a per-organization ledger with the model that actually served it; an organization that has spent its rolling daily allowance is refused before any model work begins, including when many of its turns start at once; and the conditions a human would otherwise never notice — a degraded dependency, a failing notification, a refused organization — raise an alert."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code, the observability
> configuration under `observability/`, `postgres-init/`, `GRAPH_PATTERNS.md` patterns 11, 14, 26, 35, 37 and 38, the
> constitution's Principle V, and the tests named in `quickstart.md`. It describes what the system does today.
> **Boundaries.** The per-turn safety ceilings the graph enforces (steps, tokens, per-turn cost) are feature 001; the worker
> process model, queue and crash recovery are features 003 and 004 (this feature owns whether their failures are *visible*); the
> tool-side degrade paths (a failed notification, a degraded duplicate-call store) are feature 003 and have alerts listed here;
> the delegated-run metrics are feature 007. This feature is **the measuring and limiting layer**: telemetry out, usage in,
> allowance enforced. Six defects were found by *reproducing* behavior against the real graph, the installed tracing SDK, a real
> database and a script's real startup path while writing it (B17–B20, B22) or by establishing a gap from the code and configuration (B21); ten further gaps were found
> by reading. All are in *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An operator sees how the whole system is behaving from one place (Priority: P1)

An operator opens a dashboard and sees turn volume and outcomes, latency, tokens, tool use and errors, safety-guardrail activity,
retrieval and cache health, ingestion, and each organization's allowance — covering the web service and **every** worker replica,
however many are running and wherever they run. Logs from all of them are searchable together, and a single turn can be followed
across them by one correlation id.

**Why this priority**: A system whose workers cannot be seen cannot be run. The earlier design could never see a worker at all.

**Independent Test**: Run the web service and two worker replicas; send turns through both; confirm one metrics source shows all three
processes' counts and that a log search for one turn's id returns lines from more than one process.

**Acceptance Scenarios**:

1. **Given** several processes of different kinds, **When** each starts, **Then** each begins pushing its own metrics to one shared
   aggregation point, and the monitoring system has a single target covering all of them.
2. **Given** the aggregation point is unconfigured or absent, **When** a process starts, **Then** it runs normally, with telemetry off and a log line saying so.
3. **Given** a completed, rejected, failed, timed-out or cancelled turn, **When** it ends, **Then** the outcome is counted, its
   duration observed, and — if it completed — its step count and token total recorded.
4. **Given** latency in the 0.5–120 s range and step counts of 1–15, **When** they are recorded, **Then** they fall in buckets sized for
   those ranges (not the library's millisecond-scale defaults).
5. **Given** any tool run, **When** it starts and ends, **Then** the call and any error are counted and a correlated audit line is logged
   with only a short fingerprint of the arguments and result.
6. **Given** a log line emitted anywhere during a turn or job, **When** it is written, **Then** it carries that turn's correlation id, even from deep inside code that never heard of it.
7. **Given** the provisioned dashboards, **When** they load, **Then** every metric they query exists (verified in this spec's checks).

---

### User Story 2 - Every turn's usage is recorded against the organization and the model that served it (Priority: P1)

Each completed turn writes one row: who (organization and person), which conversation, which model alias, how many tokens, what it
cost, and — when it can be found — the concrete model behind the alias. An organization can ask for its own totals (all time and the
last 24 hours, with its daily limit) and never sees another's.

**Why this priority**: Without a ledger there is nothing to enforce an allowance against and nothing to audit a bill with.

**Independent Test**: Complete a turn; read the ledger row and the organization's usage summary; complete a turn for a different
organization and confirm neither summary includes the other.

**Acceptance Scenarios**:

1. **Given** a completed turn that used tokens, **When** it ends, **Then** exactly one row is written with the tenant, principal, thread, model alias, token total, cost and resolved model.
2. **Given** an alias with a known price, **When** a row is written, **Then** cost = tokens ÷ 1000 × that price; **given** any other alias, **Then** cost is zero (true for the local models this demo runs) while tokens are always recorded.
3. **Given** no valid identity or zero tokens, **When** a turn ends, **Then** no row is written.
4. **Given** the ledger is unreachable, **When** a turn ends, **Then** the turn is unaffected and the failure is logged.
5. **Given** a caller, **When** they request their usage, **Then** they get their own organization's all-time tokens and cost, last-24-hour cost, and the daily limit — no way to name another organization.
6. **Given** the concrete model cannot be resolved, **When** a row is written, **Then** it is written without it.
7. **Given** a turn that **times out, errors or is cancelled** after spending tokens, **When** it ends, **Then** *(intended)* those tokens are recorded. **As built they are not — see B17.**
8. **Given** a completed turn, **When** its usage is recorded, **Then** *(intended)* no other request on that process is delayed. **As built a slow model-resolution call stalls the whole process — see B19.**

---

### User Story 3 - An organization cannot spend past its daily allowance, even with many turns at once (Priority: P1)

Before any model or tool work, the system compares the organization's spend over the **trailing 24 hours** (not a calendar day)
plus what its already-running turns have **reserved** against its ceiling. At or over the ceiling, the turn is refused with a clear
error and counted. At 80% it proceeds but is counted and logged. A turn that proceeds reserves its worst-case cost while it runs and
gives it back however it ends, so a burst of simultaneous turns cannot all pass on the same stale number.

**Why this priority**: A runaway or abusive tenant is a financial and availability risk to every other tenant on shared infrastructure.

**Independent Test**: Set a tiny ceiling; spend it; confirm the next turn is refused before the graph is touched; start many turns at once near the line and confirm the overshoot is bounded.

**Acceptance Scenarios**:

1. **Given** spend + reservations below the ceiling, **When** a turn starts, **Then** it proceeds.
2. **Given** spend + reservations at or above the ceiling, **When** a turn starts, **Then** it is refused with the budget-exceeded error **before** any graph, model or tool work, counted, and nothing is reserved.
3. **Given** 80% or more of the ceiling, **When** a turn starts, **Then** it proceeds, a warning counter increments and a log line carries the tenant and the figures.
4. **Given** a proceeding turn, **When** it starts, **Then** its per-turn ceiling is reserved atomically against the tenant, and released in a `finally` however the turn ends (the release itself runs off the critical path).
5. **Given** the ledger read or the reservation read/write fails, **When** a turn starts, **Then** it proceeds (a defense-in-depth layer fails open) and the failure is logged.
6. **Given** a reservation untouched for more than 5 minutes, **When** it is read, **Then** it is ignored as abandoned.
7. **Given** a worker that **died mid-turn**, **When** its tenant's next turns run, **Then** *(intended)* its abandoned reservation stops counting. **As built it is resurrected by the next reservation and persists — see B20.**
8. **Given** a paused turn that is **resumed** or a crashed turn that is **continued**, **When** it runs, **Then** *(intended)* the allowance is respected. **As built only a *new* turn is checked — see A6.**

---

### User Story 4 - A human is told about conditions they would otherwise never notice (Priority: P2)

When a dependency degrades quietly, a notification fails after the business write it announced already committed, uploads fail after
acceptance, a tenant is being refused, error or latency rates cross thresholds, or a monitored component goes down, an alert fires and is
visible to the operator; in a production deployment it reaches a person.

**Why this priority**: Constitution Principle V — a degrade path that increments a metric nobody alerts on is still silent.

**Independent Test**: Stop a dependency the alerts cover and confirm the matching alert fires within its window; stop the whole worker pool and confirm an alert fires. *(The second is not met — B21.)*

**Acceptance Scenarios**:

1. **Given** each of the thirteen rules (error rate, p95 latency, tool error rate, tenant budget, moderation spike, rate-limit spike, retrieval degradation, cache errors, checkpoint issues, failing team-channel notifications, degraded duplicate-call store, failing uploads, a scrape target down), **When** its condition holds for its window, **Then** the alert fires.
2. **Given** an alert, **When** it fires, **Then** it is visible in the monitoring and dashboard UIs; **given** a production deployment, **Then** it is also delivered to a person. **As built nothing is ever delivered — see A7.**
3. **Given** every worker process is down, **When** users send messages, **Then** *(intended)* an alert fires. **As built users get an error after 30 s while no metric changes and no rule can fire — see B21.**
4. **Given** a scheduled job (the ops digest, the sales follow-up sweep) whose push to the team channel fails, **When** the failure is counted, **Then** *(intended)* the counter reaches the monitoring system and the failing-notification alert can fire. **As built those jobs never export metrics — see B22.**

---

### User Story 5 - Logs and metrics describe what happened without recording what anyone said, and telemetry costs the process almost nothing (Priority: P2)

Logs and metrics carry classes, counts, durations, fingerprints and identifiers — never message text, tool arguments or tool
results. The one place content *is* kept is the optional per-turn trace, which exists so an operator can inspect what the model saw and said. The
telemetry layer adds no per-turn growth in threads, memory or blocking to the process it observes.

**Why this priority**: Observability that leaks content is a data-protection incident; observability that leaks resources is an outage.

**Independent Test**: Run a turn whose text and tool arguments contain a recognizable secret; search every log line and metric label for it (none should match); then run 100 turns and compare the process's thread count before and after.

**Acceptance Scenarios**:

1. **Given** a tool call, **When** it is logged, **Then** only the tool name, a 16-character fingerprint of the arguments and result, and error class names appear.
2. **Given** an unexpected failure, **When** it is logged, **Then** the line carries the exception *class*, not its text; the full text goes only to the (optional) trace.
   **Given** a traced turn, **Then** the trace holds the user's text, the model's prompts and answers and the tool inputs and outputs (tool results credential-scrubbed) — content by design, *not* metadata only (see A10).
3. **Given** metric labels, **When** they are emitted, **Then** each is a small closed set (outcome, tool, reason, stage, sink…) — never a tenant, principal, thread or content.
4. **Given** tracing is not configured (no keys), **When** a turn runs, **Then** it runs normally and nothing is sent to a tracing service.
5. **Given** 100 turns, traced or not, **When** they finish, **Then** *(intended)* the process's thread count is unchanged. **As built it grows by about six per turn — see B18.**

---

### Edge Cases

- A **paused** turn is not counted as a request when it pauses (only the resumed leg counts, with only that leg's latency); a paused turn never resumed is never counted or recorded.
- A cost of zero is normal: local models are free, so the daily and per-turn ceilings are inert unless a priced model is configured — by design, but silent (A2).
- The window is rolling (now − 24 h), so an organization's near-limit state never resets at midnight.
- Scheduled jobs record their usage under the default organization with a fixed principal and never check the allowance: they call implementation functions directly (feature 003).
- The reservation is the worst-case **ceiling** (default $0.50) for every proceeding turn, whether or not the model is priced, so with an unpriced model the reserved figure is notional.
- Metric labels are not enforced against their declaration at runtime; consistency holds today by inspection (A5).
- Tracing and its flush are best-effort and never fail a turn; a missing key just means no trace.
- The refusal event does not log which tenant tripped it; the tenant appears only in the earlier 80% warning line (A4).

## Requirements *(mandatory)*

### Functional Requirements

**Metrics and aggregation**

- **FR-001**: Every long-running process MUST push its metrics to a shared aggregation point so one scrape target covers the web service and every worker replica; telemetry MUST be configured once at process start, never at import; an unset endpoint MUST disable export without error and log that it did.
- **FR-002**: Every counter and histogram MUST be created through the shared wrapper (safe before configuration); the latency and step-count histograms MUST use explicit buckets sized for this application's ranges.
- **FR-003**: A turn's outcome (`success`, `rejected`, `error`, `timeout`, `cancelled`) MUST be counted and its latency observed; a completed turn's step count and token total MUST be recorded; tool calls and tool errors MUST be counted by a callback attached to every turn.
- **FR-004**: Every degrade-and-continue path MUST increment a metric, and every such path that can leave a human unaware of committed business state MUST have an alert rule. *(Partly met — A1, A4, B21.)*
- **FR-005**: Metric labels MUST be small closed sets and MUST NOT include a tenant, principal, thread or any content.

**Logs and traces**

- **FR-006**: Service processes MUST emit one JSON object per line with timestamp, level, logger, message, the ambient correlation id (bound once per turn or job and carried through every `await`), the call's extra fields, and exceptions as a formatted traceback string; a non-serializable extra MUST be stringified, never fatal.
- **FR-007**: Logs and metrics MUST carry metadata only: no message text, tool arguments or results (a short fingerprint instead), and only an exception's class in a log line. Traces are the one sanctioned content-bearing channel (tool results credential-scrubbed); the constitution's wording on this is out of step with the system — see A10.
- **FR-008**: Tracing MUST be optional and best-effort: no keys means no trace and no failure; a turn and each resume MUST open their own trace and flush it at the end.
- **FR-009**: The telemetry layer MUST NOT add per-turn growth in threads, memory or event-loop blocking. *(Not met — B18, B19.)*

**Dashboards and alerts**

- **FR-010**: The provisioned dashboards and the thirteen alert rules MUST reference only metrics that exist (verified here: 33 referenced names, 0 undefined).
- **FR-011**: A fired alert MUST reach a human in a production deployment. *(Not met — A7.)*
- **FR-012**: A total outage of the worker pool, or of one product's workers, MUST be visible in metrics and fire an alert. *(Not met — B21.)*
- **FR-028**: Every process that increments a metric — including each scheduled job — MUST export it. *(Not met — B22.)*

**Usage ledger**

- **FR-013**: A completed turn with a valid identity and at least one token MUST write one ledger row (tenant, principal, thread, model alias, tokens, cost, best-effort resolved model); otherwise none; a write failure MUST NOT fail the turn.
- **FR-014**: Cost MUST be tokens ÷ 1000 × the alias's price from an explicit table; an alias not in it MUST cost zero; tokens MUST always be recorded.
- **FR-015**: Tokens spent by a turn that times out, errors or is cancelled MUST also be recorded. *(Not met — B17.)*
- **FR-016**: Recording usage MUST NOT block other work on the process. *(Not met — B19.)*
- **FR-017**: `GET /usage` MUST return the caller's own organization's all-time tokens and cost, last-24-hour cost and the daily limit, and MUST NOT accept another organization's name.

**Allowance**

- **FR-018**: Before any graph work, a new turn MUST be refused with the budget-exceeded error if rolling-24-hour spend plus in-flight reservations is at or above the ceiling (counted); at or above 80% it MUST proceed with a counter and a log line carrying the figures.
- **FR-019**: A proceeding turn MUST reserve its per-turn ceiling atomically against its tenant and release it in a `finally` on every exit path (off the critical path); a reservation or ledger failure MUST fail open and be logged.
- **FR-020**: A reservation older than 5 minutes MUST be ignored when read.
- **FR-021**: A reservation held by a worker that died MUST stop counting. *(Not met — B20.)*
- **FR-022**: A resumed turn MUST respect the allowance. *(Met — A6 fixed.)* A crash-continued turn is a retry of admitted work and is NOT refused (that could strand a turn that already ran a mutating tool); it holds a reservation instead, and its overshoot is bounded by one turn's ceiling.

**Maintenance**

- **FR-023**: Each degrade path in the ledger, the allowance and model resolution MUST increment a metric. *(Met — A1 fixed.)*
- **FR-024**: A turn's cost MUST be priced by the model that actually ran it. *(Met — A2 fixed: priced per call from LiteLLM; a delegated run by its specialist's alias. Residual: a LiteLLM fallback can serve at another price unseen.)*
- **FR-025**: The rolling-window read MUST stay cheap as history grows, and the ledger MUST have a retention policy. *(Met — A3 fixed.)*
- **FR-026**: Every tunable behind this feature MUST have an entry in the example environment file. *(Not met — A8.)*
- **FR-027**: The ledger's write and read, the price table and the reservation statements MUST be covered by tests, the statements against a real database; the alert rules and dashboards MUST be validated (syntax, and that every metric they reference exists). *(Not met — A9.)*

### Key Entities *(include if feature involves data)*

- **Metric**: A named counter or histogram with a small closed label set, pushed from every process.
- **Alert Rule**: A condition over metrics with a window and a severity, evaluated by the monitoring system.
- **Usage Row**: One completed turn's tenant, principal, thread, model alias, tokens, cost, time and resolved model.
- **Tenant Allowance**: A rolling-24-hour dollar ceiling per organization (default $20) over the ledger plus reservations.
- **Reservation**: One row per tenant holding the in-flight reserved dollars and the time it was last touched.
- **Correlation Id**: A per-turn or per-job identifier bound once and stamped on every log line.
- **Trace**: An optional per-turn (and per-resume) record in the tracing service, holding the turn's input, the model's prompts and answers and tool inputs and outputs.
- **Dashboard**: A provisioned view over the metrics and logs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the web service and N worker replicas running, one metrics source shows all N+1 processes' counts; adding or removing a replica needs no monitoring change.
- **SC-002**: 0 log lines or metric labels contain message text, tool arguments, tool results or credentials.
- **SC-003**: Every *completed* turn's tokens appear exactly once in its organization's ledger.
- **SC-004**: 100% of tokens a turn spends appear in the ledger however it ends. *(Not met — B17: a 500-token turn that timed out recorded 0 rows and 0 counter.)*
- **SC-005**: An organization at or over its rolling-24-hour ceiling makes 0 model calls on a new turn.
- **SC-006**: N turns started at once for an organization just under its ceiling cannot all pass on the same stale spend figure (the reservation bounds the overshoot to the turns already in flight).
- **SC-007**: After any number of worker crashes, an idle organization's reservation reads zero and stays zero on its next turn. *(Not met — B20: a leaked 0.50 persisted after its turn finished.)*
- **SC-008**: After 100 turns the process holds the same number of threads and no extra blocking. *(Not met — B18: 60 extra threads after 10 turns, so about six per turn; B19: a 1.0 s stall for two recordings.)*
- **SC-009**: With every worker down, an alert fires within 10 minutes. *(Not met — B21.)*
- **SC-010**: Every degrade-and-continue path in this feature increments a counter. *(Not met — A1.)*
- **SC-011**: A failed push from a scheduled job is visible to the failing-notification alert. *(Not met — B22.)*

## Assumptions & Known Gaps

**Assumptions**

- Local models cost nothing; the ledger records tokens regardless and a priced model is an explicit configuration step.
- The allowance is per organization, not per person; the daily window is rolling.
- The monitoring stack is a local/demo default; a production deployment supplies its own alert receiver and sizing.
- Reservations use the per-turn ceiling as a conservative upper bound, not an estimate.

**Out of scope for this feature**

- The graph's own per-turn safety ceilings (feature 001); the queue, workers and crash recovery (003, 004); the delegated-run metrics (007).
- Billing, invoicing, per-person budgets, cost allocation to products, anomaly detection, a tracing server's own operation.

**Known gaps (disclosed, with how each was established)**

- **Bug B17 — a turn that times out, errors or is cancelled records no usage (reproduced at function level, against the real graph).** The ledger write and the token counter run only on the *completed* branch of the stream core; the timeout, error and cancel branches call the metrics helper with no state, and its comment says those branches "have total_tokens == 0 anyway". That is false for any turn that spent tokens before failing. Reproduced with the real graph and a model that returned a 500-token step and then stalled past a shortened request timeout: the stream ended with a `timeout` error, the **checkpoint held 500 tokens**, the ledger recorder was called **zero** times and the token counter did not move. The turns most likely to run long and fail are the ones that vanish from the allowance. The same shape as feature 007's B14, one level up. Not fixed here.
- **Bug B18 — every turn leaks background threads, traced or not (reproduced against the installed tracing SDK).** The trace opener builds a **new** tracing client for every turn, and the end-of-turn flush builds **another**. In the installed SDK (2.60.10) each client starts three background threads that outlive both the client and a garbage collection. Reproduced with dummy keys and an unreachable local address (nothing left the machine): 20 clients → +60 threads; 10 simulated turns (two clients each, no references kept, collection run, 1.5 s idle) → **60 threads, all daemon**, still alive. With **no keys at all** (the shipped example environment) the client is disabled yet still starts its three threads (1 → 4). A worker therefore gains about six threads per turn for its whole life in the default configuration. Not fixed here.
- **Bug B19 — recording usage blocks the event loop (reproduced at function level).** `record_usage` calls the model resolver, which makes a **synchronous** HTTP request with a 5 s timeout from inside an `async` function and caches only successes, so every failure repeats the blocking call on every completed turn. Reproduced by replacing the HTTP call with a 0.5 s blocking stand-in that fails: two recordings made two attempts and a 10 ms heartbeat saw a **1.02 s** stall. With the real 5 s timeout and a proxy that is slow, unreachable or rejects the app's key on its admin endpoint, every completed turn would stall every other turn on that worker for up to 5 s. (The rejected-key case was not run.) Not fixed here.
- **Bug B20 — a leaked reservation is resurrected, not healed (reproduced against a real database).** The staleness rule is applied when the single per-tenant row is *read*; the reserve statement is an upsert that **adds** to whatever the row holds and refreshes its timestamp. So a reservation left by a worker that died mid-turn is ignored while the tenant is idle for 5 minutes — and returns to full effect on the tenant's next turn. Reproduced with the three statements copied from the ledger module against a throwaway Postgres: reserve 0.50 (never released); back-date the row 10 minutes — the read returns **0 rows**; reserve again — the read returns **1.00**; the second turn releases its own 0.50 — the read returns **0.50** with nothing running. Each such crash leaks up to the per-turn ceiling permanently; 40 of them equal the default $20 ceiling, after which that tenant is admitted about one turn per idle 5 minutes (arithmetic, not run). The module's own comment claims it "self-heals". Not fixed here.
- **Bug B22 — scheduled jobs never export their metrics, so the failing-notification alert cannot see their failures (reproduced against the script's real startup path).** The ops digest, the sales follow-up sweep, the duplicate-call sweep and the ops investigation script call the logging configuration at startup but **never** the telemetry configuration; only the API, the agent worker, the ingest worker and the Telegram channel do. The digest and the follow-up sweep push to the team channel through the shared notifier, which counts every send by outcome — the counter the failing-notification alert (added 2026-10-01 for exactly the case "a human could go unnotified") reads. Reproduced by running the digest script's startup path (logging configuration only), incrementing that counter with `outcome="error"`, and inspecting the global meter provider: it is OpenTelemetry's **proxy** provider with no reader and no exporter, so the increment goes nowhere. The scheduled jobs are the notifier's main unattended callers, so a sustained failure of the digest push is invisible. (Whether a short-lived process would flush a periodic exporter before exit was not tested; there is no exporter to flush.) Not fixed here.
- **Bug B21 — a total worker outage is invisible to metrics and alerts (established by reading).** When no worker consumes a request, the chat reader gives up after the first-event deadline (30 s) and yields an error — and increments **no metric**. `agent_requests_total` is incremented where a turn runs (in a worker), so a request no worker picks up is never counted; the collector target stays up when workers stop (they push, nothing scrapes them); there is no consumer-lag or queue-depth metric and no `absent()`-style rule; and the readiness endpoint checks the two databases, the vector store, the queue/cache store and the ML (rerank and moderation) service — not the model proxy and not whether any consumer exists. All users see an error; the dashboard is flat and nothing fires. *(Read: the reader, `alerts.yml`, `health.py`; not exercised against a running stack.)*
- **A1 (FIXED — `agent_cost_governance_degraded_total{path}` plus two alerts; text below is the original finding) — the ledger, allowance and resolver degrade paths are log-only.** A failed ledger write ("usage ledger write failed"), a failed ledger read inside the allowance check (which makes the allowance **unenforced** for that turn), a failed reservation write/release/read and a failed model resolution each log a warning and increment no counter — Principle V requires a counter for every degrade-and-continue path, and a lost ledger row is committed spend a human will not learn about. *(Read.)*
- **A2 (FIXED — see GRAPH_PATTERNS.md pattern 26; text below is the original finding) — cost depends on a code table keyed by alias, priced by the wrong model in one case.** `PRICE_PER_1K_TOKENS_USD` holds two entries (`gpt-4o`, `gpt-4o-mini`) and any other alias costs $0 with no signal when tokens > 0 and the price is unknown. Aliases exist so a provider can be swapped by configuration (pattern 38), so pointing `chat` at a paid model through the proxy leaves cost at $0 and both ceilings inert. The in-run cost ceiling prices every step by the global default alias even for a delegated run whose specialist declares its own `model`, while its ledger row uses the specialist's alias. Pricing is one rate per total token (no input/output split). *(Read.)*
- **A3 (FIXED — index migration 17 and a retention sweep; text below is the original finding) — the rolling-window read scans the tenant's whole history, and the ledger is never trimmed.** The only ledger index is `(tenant, principal)`; the allowance check and `GET /usage` filter by `(tenant, recorded_at)`. Measured on a throwaway Postgres with 600,000 rows (200,000 for one tenant over 90 days, 2,223 in the last 24 h): the shipped query read the tenant's 200,000 index entries and **5,770 buffers in 9.3 ms**; with a `(tenant, recorded_at)` index it read **1,857 buffers in 1.5 ms**. Small today, linear in history, and run before every turn; there is no retention. *(Measured, warm cache.)*
- **A4 — 21 of the 53 metrics appear on no dashboard and in no alert, and the refusal does not name its tenant.** Among them: reclaimed jobs (crash recovery), the three circuit-breaker counters, ML moderation degradation (a safety-posture change), context-retrieval degradation and feature 007's subagent metrics. The budget-exceeded counter has no tenant label by design and the refusal logs no tenant, so an operator can find the tenant only from the earlier 80% warning. The error-rate rule counts only the `error` outcome, so a run of timeouts reaches an alert only through the p95 latency rule after 10 minutes, and feature 007's delegated-run duration histogram has no bucket view, so it keeps the SDK's millisecond-scale default buckets. *(Read; established with a script that cross-checks every metric against every alert and dashboard query.)*
- **A5 — metric labels are declared but never enforced.** The wrapper stores `labelnames` and ignores it. A static check of all 53 `.labels(...)` call sites found 0 mismatches today, so nothing is wrong now; nothing prevents a typo from creating a new series that no alert matches. *(Read; checked with an AST script.)*
- **A6 (FIXED for resume; crash-continue deliberately pre-authorized — see GRAPH_PATTERNS.md pattern 26; text below is the original finding) — only a *new* turn is checked against the allowance.** Resume and crash-continue paths do not call the check or take a reservation, so an organization over its ceiling can resume a paused turn (bounded by that turn's own ceiling). *(Read: the allowance check has one caller.)*
- **A7 — no alert is ever delivered.** The Alertmanager configuration has a receiver with no notification settings, and the **production** observability compose file mounts the same file. The comment (and the README) say this is the local/demo default; nothing marks the production file as incomplete. *(Read.)*
- **A8 — configuration and comment drift.** `MAX_COST_USD_PER_TURN`, `MAX_COST_USD_PER_TENANT_PER_DAY` and `REQUEST_TIMEOUT_SECONDS` are settings with no example-environment entry; the comment on the request counter lists four outcomes though the code also emits `cancelled`; and a paused turn is never counted as a request. *(Read.)*
- **A9 (ledger statements FIXED — real-Postgres tests; alert/dashboard validators were added separately in `tests/core/test_alert_rules.py`) — the ledger's own statements are untested.** Nothing tests `record_usage` (its parameters, the price table, the no-op rules) or `usage_summary` (its `WHERE` clause); the reservation tests assert statement shape against a fake cursor — including one that a stale reservation is excluded at read time — and none runs against a real database, which is how B20 shipped. Likewise nothing validates the alert rules or the dashboards — no rule-syntax check in CI and no test that every metric they name exists; this spec's own cross-check script is the only such check. *(Read: grep of the tests, the Makefile and the workflows.)*
- **A10 — the constitution says traces carry metadata only; the system's traces carry content.** Principle V: "Logs and traces carry metadata only … never message content or the state dict." The trace opener passes the user's text as the trace input, the stream core writes the final answer as its output, and the LangChain integration records every prompt, completion and tool input and output; a test even asserts the trace shows the text the client saw. `GRAPH_PATTERNS.md` pattern 14 is consistent with the code, not the constitution: logs never carry content "outside Langfuse". Tool results are credential-scrubbed on the way in; the user's own text and the final answer are not. The two cannot both stand: either amend the principle (traces are the one access-controlled, content-bearing channel, with a stated retention and scrubbing rule) through the constitution process, or strip content from traces. *(Read: the constitution, the trace opener, the pattern text.)* Not decided here.
