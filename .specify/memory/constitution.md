# agent-core-demo Constitution

## Core Principles

### I. Fail-Closed Tenant Isolation (NON-NEGOTIABLE)
Every read and write MUST be scoped to the caller's tenant (and, for personal data, owner).
- A `SecurityCtx` (`tenant`, `principal`, `claims`) MUST come only from
  `config["configurable"]["ctx"]`, stamped once upstream. It MUST NOT be derived from message
  content, tool arguments, or model output.
- Scoping MUST be enforced inside the store query (`WHERE tenant = %s`, a Qdrant pre-filter via
  `Policy.lower`). A Python post-filter over an unscoped result is forbidden.
- Every tool that touches tenant data MUST independently check ctx (`_ctx_or_refuse` /
  `valid_ctx`) and refuse when it is missing or malformed. It MUST NOT rely on an upstream node
  having already checked.
- A child table MUST carry its own `tenant` column rather than inherit it solely via a join.
- Unknown or missing security state MUST resolve to refusal, never to a default tenant.

Rationale: a buggy post-filter and a correct one look identical until a cross-tenant leak
appears on an untested query; a store-native predicate fails loudly instead.

### II. Mandatory Human Approval for Side Effects (NON-NEGOTIABLE)
Every tool MUST declare `read_only`, `mutating`, or `outward` in its `TOOL_CAPABILITIES`.
- Any non-`read_only` call MUST route through `human_approval` unconditionally. No flag, env
  var, or per-domain setting may bypass it.
- A tool missing from the capability mapping MUST be treated as `outward` (fail closed).
- Unattended callers (Telegram long-poll, fire-and-forget queue jobs) MUST auto-decline a pause
  via `astream_events_turn_unattended`; they MUST NOT auto-approve.
- Subagents MUST be restricted to `read_only` tools, enforced at catalog-build time, and MUST
  NOT be able to spawn subagents.
- Cron or other unattended jobs that need a write MUST call the domain's `_impl` functions
  directly as a fixed pipeline. They MUST NOT enter the tool-calling agent loop.

Rationale: a RAG agent is exposed to untrusted content on nearly every turn; write capability
plus untrusted content is one gamble away from a document steering a real write.

### III. Fixed, Typed Tools — Never Generated Operations
A tool is a closed, typed, reviewable operation; the model chooses parameters, never
constructs the operation.
- Every tool MUST declare an explicit Pydantic `args_schema`; free-form fields MUST have
  length and format bounds, and categorical fields MUST be closed enums.
- Queries MUST be parameterized, with fixed SQL text. No tool may expose `execute(sql)`,
  `eval`, or a model-written query or path as a write target.
- A write tool's target identity (row id, Qdrant point id) MUST be derived by code, never
  supplied by the model.
- A broad third-party tool surface (e.g. OpenSandbox's lifecycle API) MUST be wrapped behind a
  small, flat, purpose-built tool set. The raw catalog MUST NOT be handed to the model.
- A model-capability failure MUST be checked first for a structural cause (tool order, schema
  shape, argument passing) before it is "fixed" with more prompt text.

Rationale: the access boundary must live in code a reviewer can read, not in a string the
model writes.

### IV. Exactly-Once Side Effects (NON-NEGOTIABLE)
Every `mutating`/`outward` tool MUST be safe to run twice for the same logical action.
- The tool call MUST be wrapped in `idempotent()` (`app/agent/tool_idempotency.py`), keyed by the
  provider-assigned `tool_call_id` obtained via `InjectedToolCallId`.
- The write itself MUST also be idempotent at the target, as the second layer: a nullable
  `tool_call_id UNIQUE` column with `ON CONFLICT DO NOTHING`, or a deterministic id
  (`uuid5` / content-addressed) instead of `uuid4()`.
- An "append" MUST be an inserted row keyed by `tool_call_id`, never a concatenation onto a
  shared TEXT column. Aggregate text at read time (`STRING_AGG`).
- A tool's own soft timeout MUST surface as `MutatingToolTimedOut` and steer the agent to
  verify via a read-only tool. A timed-out write MUST NOT be retried blindly: the retry
  arrives under a new `tool_call_id` that no id-keyed defense can recognize.
- Automatic retries MUST be limited to exceptions the caller proves mean "the call never
  landed". A bare `except Exception` retry is forbidden.
- Crash recovery MUST continue the checkpointed run (`astream_events_continue_turn`); it MUST
  NOT restart a turn that already ran a mutating/outward call, because a restart re-asks the
  LLM and mints unrecognizable ids.
- Dedup-store failure MUST fail open (log + metric), because it is a defense-in-depth layer
  and not a precondition for a tool to work.

Rationale: this app has needed repeated audit rounds to close duplicate-side-effect gaps, and
each round found a window the previous keying could not see. New write paths MUST state their
duplicate story up front.

### V. Bounded, Observable Failure
No loop, wait, retry, or degradation may be unbounded or silent.
- Every loop and blocking wait MUST have an explicit ceiling (iteration, tool-call fan-out,
  token, cost, repeated-action, per-tool timeout, whole-turn timeout, history size, graph
  recursion limit, `first_event_deadline_seconds`, `MAX_AUTO_RECLAIM_RETRIES`). A wait that can
  only end when a counterparty acts MUST also have a deadline.
- Every dependency-failure path MUST pick its policy deliberately and document it: enrichment
  degrades, a failed LLM call retries, a security check fails closed, a defense-in-depth store
  fails open.
- Every degrade-and-continue path MUST increment a metric (`app/core/metrics.py`). A path that
  can leave a human unaware of committed business state (a failed notification, a degraded
  dedup store, a failed upload) MUST also have a Prometheus alert rule in
  `observability/prometheus/alerts.yml`.
- A deliberate broad `except Exception` MUST carry `# noqa: BLE001 - <reason>`. A silent
  `except: pass` is forbidden except for a justified `# noqa: S110`.
- A multi-step operation that fails part-way MUST compensate for what it already did (release
  the claim, delete the orphaned blob). It MUST NOT abort a whole batch for one item's failure.
- Logs and traces carry metadata only (node, `run_id`, duration, outcome), never message content
  or the state dict. Caller-facing errors MUST use the `ErrorCode` envelope
  (`app/core/errors.py`).

Rationale: the recurring real bugs here were silent hangs, orphans, and failure paths nobody
watched — not crashes.

### VI. Untrusted Content Is Data
Anything not authored by this codebase is data, never instructions.
- Retrieved documents MUST be framed in `<retrieved_document>` delimiters, with the system
  prompt stating once that delimited text is data.
- Tool output MUST pass through credential scrubbing (`app/core/scrubbing.py`) before reaching a
  prompt or trace. Server-side fetches of a user-supplied URL MUST use the shared SSRF guard
  (`app/core/url_safety.py`).
- Input moderation MUST run before any retrieval or LLM spend.
- Cross-session memory MUST be written only by the gated `remember` tool and re-filtered by
  tenant and owner on every read. No code may extract memories from turn text on its own.
- `SYSTEM_PROMPT` MUST remain a ctx-free constant: no tenant, principal, timestamp, or other
  per-request value is interpolated into it (prompt-cache stability).
- Facts about what the model did (which sources it cited, whether it called a tool) MUST be
  computed from the output and the graph state, never taken from the model's self-report.

Rationale: a poisoned document affects one answer; a poisoned memory replays on every later
turn until removed.

### VII. Test Discipline With Real Backends Where They Matter
Behavior is proven at the cheapest tier that can actually prove it.
- The default suite (`make test`) MUST be hermetic: a fake LLM and no live Postgres, Redis,
  Qdrant, or ml-service. Its outcome MUST NOT depend on which containers happen to be running on
  the machine. A new live-service call on the turn path MUST come with an autouse mock in
  `tests/conftest.py`.
- A bug fix MUST land with a regression test that fails without the fix.
- Tests needing a real service MUST carry the matching marker (`integration`, `llm`, `e2e`,
  `deepeval`, `crawl`, `sandbox`) and MUST self-skip, never fail, when Docker or the model is
  unreachable. The default `-m` selection in `pyproject.toml` MUST keep a bare `pytest -q` fast.
- A mock proves statement shape, not database or broker behavior. Code that depends on a real
  `UNIQUE` / `ON CONFLICT` constraint, consumer-group semantics, or checkpointer behavior MUST
  have its reliance on that behavior stated in a comment, and SHOULD gain an `integration`-tier
  test. Today the store tests (`tests/domains/*/test_store.py`) use fake cursors and assert the
  SQL text only; that gap is known and is not to be described as proven.
- A prompt, model-alias, or retrieval change MUST pass `make promptfoo` and the golden-dataset
  `make eval` before release. LLM-judged signals (deepeval, garak, promptfoo-redteam) are
  advisory and MUST NOT be promoted to a hard gate while the judge model is unreliable.
- Claims about third-party behavior (a library's locking, a framework's resume semantics) MUST
  be verified against the installed source or a real run, not assumed.

Rationale: every tier exists because a lower one missed a real bug; the hermetic tier stays
fast only if nothing in it can quietly reach a live service.

### VIII. Why-First Documentation and Honest Gaps
Documentation records why, and the failure that motivated the code.
- A new architectural pattern MUST be added to `GRAPH_PATTERNS.md` as a numbered entry that
  states the bug or gotcha behind it. A change that closes a gap MUST update the matching
  "Extending Further" entry.
- Known limitations MUST be disclosed in the README "Roadmap" or "Extending Further", never
  omitted or papered over. A fix that deliberately leaves an adjacent gap open MUST name it.
- Module docstrings and non-obvious comments MUST explain the invariant and the reason, not
  restate the code. Long-form comments are intentional; the lint config deliberately sets no
  line-length rule.
- A DB invariant that is true but invisible (e.g. `get_connection()`'s `async with` is a
  transaction) MUST be written down where the next editor will read it.

Rationale: this repo is also a teaching reference; a pattern without its motivating failure
cannot be judged or safely changed later.

## Architecture & Technology Constraints

- **Runtime**: Python 3.13 (Dockerfile and CI). Tool, store, and client I/O is async-first. The
  durable checkpointer (`AsyncPostgresSaver`) MUST be opened on the same event loop that uses it.
  `MemorySaver` is for tests and ephemeral subagent runs only.
- **LLM access**: every chat and embedding call goes through the LiteLLM proxy
  (`OPENAI_API_BASE`) using model aliases (`chat`, `embed`), never a provider model name. The core
  MUST run fully offline against local Ollama. A cloud model MAY be used only for eval and
  red-team grading roles, never as the target model or a runtime dependency.
- **Stores**: Postgres (`appdata`, plus a dedicated checkpointer database), Qdrant (hybrid
  dense + BM25 retrieval, with the cross-encoder rerank layered on top), Redis (Streams queues,
  semantic cache), MinIO (uploads).
  Appdata access goes through `app/agent/sql_store.py`'s pooled `get_connection()`; the only
  other direct psycopg users are the checkpointer pool (`app/agent/runtime.py`) and the health
  probe (`app/api/health.py`).
- **Schema changes**: add a new numbered `postgres-init/NN-*.sql` that continues the sequence.
  Prefer a new script over editing an applied one. State in the script's header how an existing
  volume applies it (init scripts only auto-run on a fresh volume).
- **Checkpoint compatibility**: a `State` or graph-topology change that is not backward
  compatible MUST bump `STATE_SCHEMA_VERSION` (`app/agent/graph.py`); a differing build SHA
  alone is not a break.
- **Composition**: a new use case is an `AgentManifest` + `DomainPlugin` under
  `app/domains/<name>/` (`store.py`, `tools.py`, `domain.py`). It MUST NOT fork or branch
  `build_graph()`. A domain's `allowed_tools` is its sandbox boundary; skills and subagents are
  domain-tagged so they never leak across domains.
- **Queueing**: the HTTP chat path is the Redis Streams queue; domain is inferred from the
  stream a job landed on, never from its payload. A queue consumer is at-least-once, so its
  handler MUST be safe under redelivery (Principle IV).
- **Configuration**: tunables live in `app/core/config.py` `Settings` (pydantic-settings, read
  from `.env`) with a matching `.env.example` entry. Secrets (`.env`, `CREDENTIALS.local.md`)
  MUST NOT be committed or echoed into logs, traces, or docs.
- **Dependencies**: `requirements.txt` is the human-edited source and every non-obvious pin
  carries a reason comment; `requirements-lock.txt` is machine-generated and is what Docker and
  CI install; dev-only tools live in `requirements-dev.txt`.
- **Observability**: structlog JSON logs correlated by `run_id`; metrics via
  `app/core/metrics.py` pushed over OTLP with explicit histogram buckets; Langfuse tracing keyed
  by `thread_id`. Every node is wrapped by `_instrumented` at registration time in
  `build_graph`, never inside the node body.

## Development Workflow & Quality Gates

- **Branches and PRs**: work on a topic branch (`fix/…`, `feat/…`) off `main` and merge via pull
  request. Commit subjects use a conventional prefix (`fix:`, `feat:`, `test:`, `docs:`,
  `infra:`). The body states the failure mode, the root cause, and anything deliberately left
  undone.
- **Required before a PR**: `make lint` (ruff: F, I, UP, B, BLE, S110), `make typecheck` (mypy
  over `app/` and `scripts/`), and `make test` MUST pass; CI runs the same commands. Run
  `make test-integration` (Docker) for changes to stores, queues, workers, or SQL.
- **Type-ignore and noqa**: `# type: ignore[code]` and `# noqa: <code>` MUST carry a reason
  comment at the site.
- **Security gates**: trivy, semgrep, and checkov findings are fixed or explicitly justified in
  the relevant config, never silenced without a stated reason. `make eval`, `make garak`,
  `make deepeval`, and `make promptfoo-redteam` are deliberate, manual, or non-blocking by design
  (see Principle VII).
- **Spec Kit gates**: a plan's Constitution Check MUST address each principle the feature
  touches. A feature that adds a `mutating`/`outward` tool MUST state, in its spec or plan, the
  tool's capability tier, its tenant scoping, and its duplicate-side-effect (idempotency)
  story. A feature that adds a store or queue consumer MUST state its failure policy and the
  metric/alert covering it.
- **Definition of done**: a behavior change ships with tests at the right tier, the matching
  `GRAPH_PATTERNS.md` / README updates (Principle VIII), and, where a degrade path was added, its
  metric and alert rule (Principle V).

## Governance

This constitution supersedes ad-hoc practice and individual preference. When it conflicts with
a description in `GRAPH_PATTERNS.md` or the README, the constitution states the intent and the
conflicting document MUST be corrected.

- **Amendments**: change this file in a pull request that states the rationale, the Sync Impact
  Report, and, for any rule that existing code violates, a migration plan listing the violating
  sites. Principles I, II, and IV are NON-NEGOTIABLE: weakening one requires a MAJOR version
  and explicit maintainer sign-off in the PR.
- **Versioning**: semantic. MAJOR for a removed or redefined principle or a weakened
  NON-NEGOTIABLE rule; MINOR for a new principle or section or materially expanded guidance;
  PATCH for wording and clarifications.
- **Compliance review**: every PR description and review MUST confirm the change against the
  principles it touches. A violation that cannot be avoided MUST be recorded and justified in
  the plan's Complexity Tracking table, not merged silently. Complexity beyond what a principle
  requires MUST be justified.
- **Runtime guidance**: day-to-day development guidance for agents and contributors lives in
  `CLAUDE.md` and `.claude/rules/`; those files MUST NOT contradict this constitution.

**Version**: 1.0.0 | **Ratified**: 2026-10-01 | **Last Amended**: 2026-10-01
