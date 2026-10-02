---

description: "Task list for feature 002 — Tenant Isolation and Cross-Session Memory (retrospective)"
---

# Tasks: Tenant Isolation and Cross-Session Memory

**Input**: Design documents from `/specs/002-tenant-isolation-and-memory/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Constitution Principle VII requires a regression test for every bug fix and
real-backend proof where behavior depends on a store; for *this* feature the real-backend tier is
the one that is missing (plan A4), so test tasks are central, not optional.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is
  written first** (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids: **B2** conversation-ownership defect · **A3** no deletion entry point · **A4** real-backend
  isolation proof only partial · **A5** no authentication/header handling in the shipped proxy ·
  **E1** ungoverned, deliberately-global ops domain · **R3** policy-contract wording. See
  plan.md *Complexity Tracking* and research.md *Deferred*.
- Tasks that need Docker (`integration` tier) say so. Paths are repo-relative.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US6 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Settings `default_tenant` (`ecorp`) and `memory_retention_days` (**365**) in `app/core/config.py`, each mirrored in `.env.example`
- [x] T002 [P] Two seeded tenants `ecorp` and `other-co` ("purely so a cross-tenant isolation test has something real to prove against") in `postgres-init/02-appdata.sql`; fixed ingest identity `make-ingest` under `DEFAULT_TENANT` in `scripts/seed.py`
- [x] T003 [P] Session directory schema — `thread_id` PRIMARY KEY, `tenant`/`principal` NOT NULL, `domain` NOT NULL DEFAULT `'ecorp'`, index `(tenant, principal, domain, last_active_at DESC)` — in `postgres-init/06-chat-sessions.sql` and `postgres-init/11-chat-sessions-domain.sql`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The identity and policy primitives every story depends on. **No story work before this.**

- [x] T004 `SecurityCtx` TypedDict (`tenant`, `principal`, `claims` opaque), `Policy` Protocol, `TenantIsolationPolicy` (`permit` default-deny over `search|write_note|recall_memory|write_memory|query_structured_data`; `lower` for `documents`/`memories`), `DEFAULT_POLICY`, `valid_ctx` `TypeGuard` in `app/core/security.py`
- [x] T005 [P] `ActionAllowlistPolicy` (`permit` = action in allowlist ∧ `valid_ctx`; `lower` raises `NotImplementedError`) in `app/domains/policy.py`
- [x] T006 [P] `_ctx_from_config`, `_ctx_or_refuse` (never raises; returns `None` on missing/malformed ctx or a denied action), `_NO_CTX_REFUSAL` in `app/agent/tools.py`
- [x] T007 [P] Required identity headers — `get_ctx` declares `X-Tenant-Id` and `X-Principal-Id` as required `Header(...)` so a request missing either is a 422 before any handler runs — and `get_domain` (default `ecorp`, unknown ⇒ 422) in `app/api/main.py`
- [x] T008 [P] Counters `agent_missing_ctx_total`, `agent_memory_deletion_total{outcome}`, `agent_ingest_refused_total{reason}` in `app/core/metrics.py`
- [x] T009 [P] Scoped-store plumbing: `_build_filter` (tenant filter ANDed with `topic` and `HasIdCondition`), `build_point`, `delete_by_filter`, `count_by_filter` in `app/retrieval/qdrant_store.py`

**Checkpoint**: Foundation ready — a ctx can be stamped, checked and lowered into a store predicate.

---

## Phase 3: User Story 1 — One customer never sees another customer's data (Priority: P1) 🎯 MVP

**Goal**: Every read — documents, structured rows, cached answers, session list — returns only the
caller's tenant.

**Independent Test**: Two tenants, same questions: each sees only its own (quickstart Tier 3 steps 1–3,
Tier 1 `test_security.py`, `test_sql_store.py`).

### Tests for User Story 1

- [x] T010 [P] [US1] Document vs memory filters: tenant + kind (+ owner + retention range for memories); documents never carry a range; the two filters never overlap — `tests/core/test_security.py` (`TestTenantIsolationPolicyLower`)
- [x] T011 [P] [US1] Hybrid search keeps the tenant filter in each leg and degrades without losing scope in `tests/retrieval/test_qdrant_store.py`
- [x] T012 [P] [US1] `query_employees` always tenant-scoped; department/name filters AND onto it; two tenants ⇒ different params in `tests/agent/test_sql_store.py`
- [x] T013 [P] [US1] Relational stores include the tenant on every query/join in `tests/domains/support/test_store.py`, `tests/domains/sales/test_store.py` (fake cursors — prove SQL shape, not constraint behavior)
- [x] T014 [P] [US1] MCP `query_employees` refuses without identity and passes the tenant through in `tests/mcp/test_mcp_server.py`
- [x] T015 [P] [US1] Different tenants never collide onto one ingested point id in `tests/ingestion/test_ingestor.py::…different_tenants_never_collide_onto_the_same_point_id`

### Implementation for User Story 1

- [x] T016 [P] [US1] `_document_hits` (`DEFAULT_POLICY.lower(ctx, "documents")`; `doc_ids`/`topic` narrow only; de-dup by parent) and `search_docs` in `app/agent/tools.py`
- [x] T017 [P] [US1] Fixed parameterized `query_employees` — `WHERE tenant = %s`, optional `department =`/`name ILIKE`, `LIMIT cap+1` — in `app/agent/sql_store.py`; tool with closed `Department` enum, per-tool cap 20 and a `[truncated: …]` marker in `app/agent/tools.py`
- [x] T018 [P] [US1] Tenant column on every relational table incl. child tables, and repeated in joins — `postgres-init/07-support-tickets.sql`, `08-crm.sql`, `15-append-notes-as-rows.sql`; `app/domains/support/store.py`, `app/domains/sales/store.py`
- [x] T019 [P] [US1] Cache scoped to `@tenant:{T} @principal:{P}` with `_escape_tag` on both in `app/retrieval/semantic_cache.py`
- [x] T020 [P] [US1] Content-addressed ids `uuid5(ns, f"{tenant}|{source}|{index}|{sha256(text)}")` in `app/ingestion/ingestor.py::_content_point_id`
- [x] T021 [P] [US1] `GET /usage` returns only the caller's own tenant's all-time and rolling-24 h spend (`usage_summary(ctx["tenant"], …)`; no way to query another tenant's) in `app/api/main.py`; tenant-scoped `record_usage`/`usage_summary` in `app/agent/usage_ledger.py`; tested in `tests/api/test_api.py` (the `usage` handler) and `tests/agent/test_usage_ledger.py` (**FR-013**)

### Open follow-ups for User Story 1 (not built)

- [ ] T022 [US1] **A4 — extend the real-backend isolation proof to what is not covered** in a new `tests/integration/test_isolation_real_backends.py` (`integration` marker; needs Docker; uses `tests/containers.py::ensure_qdrant`, `ensure_postgres`, `ensure_redis`; follow the fixtures in `tests/agent/test_concurrent_turns.py`, which **already** proves document search across 6 concurrent tenants on a real Qdrant and the answer cache's tenant axis on a real Redis Stack — do not duplicate those). Seed **two tenants with overlapping content and two principals in one tenant**, then assert per [contracts/scoping-matrix.md](./contracts/scoping-matrix.md) only the missing parts: memory search as P1 returns no `owner = P2` point; a `HasId` of a B point returns nothing for A; `query_employees("A")` returns no B rows and `get_ticket("A", <B ticket id>)` is `None`; `list_sessions`/`session_belongs_to` scope by tenant+principal+domain; cache `get` as `(A, P2)` after `set` as `(A, P1)` is a miss (the principal axis). Must self-skip, never fail, when Docker is unreachable
- [ ] T023 [US1] **A4 — verify the two comment-only third-party behaviors** with the same test file: (a) a memory point with **no** `created_at` is *not* returned by the retention range filter; (b) a `HasIdCondition` combined with the tenant filter can only narrow. Record the observed result (and the Qdrant version, `qdrant-client==1.19.0` against the container image tag) in the comment above the retention filter in `app/core/security.py` and in `GRAPH_PATTERNS.md` pattern 33, replacing "Qdrant treats a missing field as never matching" with the verified statement. If (a) is false, the spec's Edge Case about unstamped memories is wrong and must be corrected
- [ ] T024 [P] [US1] **R3 — correct the policy contract**: either reword the `Policy` Protocol docstring ("no I/O, no clock, no randomness") in `app/core/security.py` to admit that `lower` reads the clock for the retention cutoff, or give `lower` an optional `now: datetime | None = None` argument so tests can pin it; update `tests/core/test_security.py` (`test_memories_target_also_includes_a_retention_horizon_range`) accordingly
- [ ] T025 [P] [US1] **Performance note (research R18)** — no payload indexes exist: measure a tenant+kind+owner+`created_at` filtered hybrid search on a realistic corpus and, if warranted, create payload indexes in `app/retrieval/qdrant_store.py::ensure_collection`; correctness is unaffected, so this is non-blocking

**Checkpoint**: after T022–T023, Principle I is *proven* for reads (not just believed).

---

## Phase 4: User Story 2 — Identity is established once, at the edge, and cannot be talked into changing (Priority: P1)

**Goal**: Identity is stamped once at a trusted boundary and read-only; missing/malformed ⇒ refuse.

**Independent Test**: Quickstart Tier 3 step 4 (422), Tier 1 `test_routing.py::TestRouteAfterValidationCtx`,
`test_mcp_server.py`.

### Tests for User Story 2

- [x] T026 [P] [US2] `valid_ctx` for `None`/`{}`/missing/empty tenant or principal; `permit` default-deny, never raises, deterministic — `tests/core/test_security.py` (`TestValidCtx`, `TestTenantIsolationPolicyPermit`, `TestDefaultPolicy`)
- [x] T027 [P] [US2] Identity checked before empty-input; `reject_context` increments `agent_missing_ctx_total` in `tests/agent/test_routing.py::TestRouteAfterValidationCtx`
- [x] T028 [P] [US2] The built-in UI sends the identity header names (a string check on the page — **not** an assertion that a request lacking them is rejected); unknown `X-Domain` ⇒ 422; a known domain passes through — `tests/api/test_api.py`

### Implementation for User Story 2

- [x] T029 [US2] `validate_input` stamps `ctx` from `config["configurable"]["ctx"]` only; `route_after_validation` → `reject_context` ("I couldn't verify who's asking…") in `app/agent/graph.py`
- [x] T030 [P] [US2] Local stamping: CLI `_LOCAL_CTX` (`DEFAULT_TENANT`, `local:<OS user>`) in `app/channels/chat.py`; Telegram `_ctx_for_user` (`DEFAULT_TENANT`, `telegram:<user_id>`) in `app/channels/telegram.py`
- [x] T031 [P] [US2] External tool server with explicit `tenant`/`principal` arguments checked by `DEFAULT_POLICY.permit("query_structured_data", ctx)` in `app/mcp/server.py`

### Open follow-ups for User Story 2 (not built)

- [ ] T032 [P] [US2] **A5 — deployment warning (docs-only, do first)**: add a prominent *"Identity headers are not authentication"* subsection to README *Deploying to production* and to `infra/README.md` stating that the shipped `Caddyfile` forwards to the API with **no** authentication and does not set or strip `X-Tenant-Id` / `X-Principal-Id`, so a deployment MUST put an authenticating gateway in front that sets both and discards client-supplied copies; add a commented example (`request_header -X-Tenant-Id` / `-X-Principal-Id` + a `forward_auth` stub) to `Caddyfile`; mention it in `.env.prod.example`
- [ ] T033 [US2] **E1 — decide and record**: should a tenant be allowed to name any registered `X-Domain`? Either (a) add a per-tenant domain allowlist checked in `app/api/main.py::get_domain` (config in `app/core/config.py` + `.env.example`; failing test first in `tests/api/test_api.py`), or (b) carve a documented exception for the deliberately global `ops_incidents` data into `.specify/memory/constitution.md` Principle I through `/speckit-constitution` and a PR (a constitution amendment is MINOR or PATCH, never weakening a NON-NEGOTIABLE rule without maintainer sign-off). Record the decision in research.md R18 and delete plan row **E1**
- [ ] T034 [US2] **FR-003 — write the missing HTTP test (found by `/speckit-analyze`; hermetic)**: no test asserts that a request lacking an identity header is rejected — the existing `test_sends_the_trusted_identity_headers` only checks that the UI page mentions the header names. Add to `tests/api/test_api.py` a test using `fastapi.testclient.TestClient(app)` (constructed *without* `with`, so the lifespan does not run, as the file already assumes) that `GET /usage` and `POST /chat/stream/queued` return **422** when `X-Tenant-Id` is absent, when `X-Principal-Id` is absent, and when both are absent, and that an *empty* header value is not accepted as a tenant. Pins FR-003, US2 scenario 1 and SC-002's first refusal path

**Checkpoint**: US2 is complete as to *shape*; real authentication remains out of scope (README Roadmap).

---

## Phase 5: User Story 3 — Personal data stays personal, even inside one organization (Priority: P1)

**Goal**: Memories and conversations are owner-scoped; another person's conversation reads as "not found".

**Independent Test**: Quickstart Tier 3 steps 5–6 (list scoping; identical 404), Tier 1 `test_sessions.py`.

### Tests for User Story 3

- [x] T035 [P] [US3] Session upsert writes thread/tenant/principal/title/domain and on conflict touches only `last_active_at`; list scopes to tenant+principal+domain; `session_belongs_to` true/false; invalid ctx never queries — `tests/agent/test_sessions.py`
- [x] T036 [P] [US3] Transcript replay returns user/assistant text and a `system` breadcrumb, omits tool plumbing in `tests/agent/test_session_messages.py`
- [x] T037 [P] [US3] Session endpoints: list passes the domain through; transcript 404s when owned by a different domain in `tests/api/test_api.py`

### Implementation for User Story 3

- [x] T038 [US3] `upsert_session` (best-effort, at turn start), `list_sessions`, `session_belongs_to` in `app/agent/sessions.py`
- [x] T039 [US3] `GET /chat/sessions`, `GET /chat/sessions/{thread_id}/messages`, `GET /chat/sessions/{thread_id}/pending_approval` (owner check first; **404 `{"detail":"session not found"}`** for both "not yours" and "does not exist") in `app/api/main.py`; `get_session_messages`/`get_pending_approval` (no ctx by design) in `app/agent/runtime_stream.py`

### Open follow-ups for User Story 3 (not built) — **B2**

> Do T040–T041 before any fix. Candidate designs and their trade-offs: research.md R15; acceptance
> criteria: [contracts/conversation-ownership.md](./contracts/conversation-ownership.md).

- [ ] T040 [US3] **B2 — write the failing tests first** (hermetic, handlers called directly per the repo's style in `tests/api/test_api.py`): add `tests/api/test_conversation_ownership.py` asserting that for a `thread_id` owned by tenant A / principal alice, a request from tenant B / principal mallory to `chat_stream_queued`, `chat_resume` and `chat_cancel` (a) is rejected with **404 `session not found`** (identical to the read endpoints), and (b) does **not** call `queue.publish_request` / `publish_resume_request` / `publish_cancel_request` nor `set_cancel_flag`; plus a positive case that a **brand-new** `thread_id` is accepted and claimed by its first caller. Add the worker-level twin in `tests/job_queue/test_agent_worker.py`: a `process_request` job whose `ctx` does not own `thread_id` publishes an `error` event and never invokes its handler. All must fail on current code
- [ ] T041 [US3] **B2 — decide the design** (record in research.md R15): (1) namespace the checkpoint key at the API boundary (`thread_id = hash(tenant, principal, domain, client_thread_id)`) so a client cannot address another's checkpoint — also changes the keys in `app/job_queue/queue.py` (`thread_lock_key`, `cancel_flag_key`), `app/agent/sessions.py`, and Telegram's `_thread_id_for_chat`, and needs a legacy-id read-through; or (2) check ownership on every thread-addressed endpoint with atomic claim-on-first-use (`INSERT … ON CONFLICT DO NOTHING` into `chat_sessions` before publishing, then compare owner); plus (3) worker-side verification as the safety net under either. State the migration story for existing conversations
- [ ] T042 [US3] **B2 — implement the API-side fix** chosen in T041 in `app/api/main.py` (`chat_stream_queued`, `chat_resume`, `chat_cancel`) and, if needed, `app/agent/sessions.py` (a `claim_or_check_session(ctx, thread_id, domain)` helper returning owned/claimed/foreign). Reuse the existing 404 so existence is not disclosed. Keep PR ≤ ~400 hand-written lines; split the migration into its own PR if it exceeds that
- [ ] T043 [US3] **B2 — worker-side defense in depth** in `app/job_queue/agent_worker.py::process_request`: before running any job kind (`turn`, `turn_continue`, `resume`, `cancel`) verify `session_belongs_to(payload["ctx"], payload["thread_id"], AGENT_DOMAIN)` (allowing a first-use claim for `turn`), and on failure publish an `ErrorEnvelope` (feature 001 A2 shape — generic message) and ack; makes T040's worker test pass
- [ ] T044 [P] [US3] **B2 — Telegram regression**: add a test to the existing `tests/channels/test_telegram_channel.py` that `app/channels/telegram.py`'s derived `telegram:<chat_id>` thread still resolves to the same conversation for the same user after the fix, and that the same id cannot be continued through the HTTP API by a different tenant
- [ ] T045 [US3] **B2 — docs and disclosure**: update `GRAPH_PATTERNS.md` pattern 17 ("Multi-Tenant Isolation") and pattern 49 with the ownership rule, remove the *Known gaps* B2 paragraph in spec.md and the **B2** row in plan.md *Complexity Tracking*, update [contracts/conversation-ownership.md](./contracts/conversation-ownership.md) "As built" column, and put the failure mode (a caller supplying another's `thread_id` continued, approved or cancelled that conversation) and root cause (the checkpoint, lock and cancel flag are keyed by `thread_id` alone) in the commit body
- [ ] T046 [P] [US3] **FR-012 — write the missing pending-approval endpoint test (found by `/speckit-analyze`; hermetic)**: `GET /chat/sessions/{thread_id}/pending_approval` has no API-level test (`grep pending_approval tests/api/test_api.py` is empty). Add to `tests/api/test_api.py`, mirroring the transcript endpoint's existing tests (`test_404s_when_owned_by_a_different_domain`): an owned thread returns the `get_pending_approval` result (`{tool_calls, resumable}`), and a thread owned by another principal, another tenant, another domain, or nobody returns the identical **404 `session not found`** without calling `get_pending_approval`

**Checkpoint**: US3 equals FR-012 only after T040–T045; until then SC-008 holds for reads, not for continuation.

---

## Phase 6: User Story 4 — The assistant remembers across conversations — only when approved (Priority: P2)

**Goal**: Memories are created only by the approved `remember` tool and recalled automatically,
re-filtered every time.

**Independent Test**: Quickstart Tier 3 steps 7–11; Tier 1 `test_tools.py` (`TestRememberArgsValidation`,
`TestRememberImpl`).

### Tests for User Story 4

- [x] T047 [P] [US4] `remember` argument validation (blank rejected, >2000 chars rejected) and `_remember_impl` writes an owner-stamped memory with a `tool_call_id`-derived id in `tests/agent/test_tools.py` (`TestRememberArgsValidation`, `TestRememberImpl`)
- [x] T048 [P] [US4] `add_note`/`remember` registered in `TOOLS` and declared `mutating` in `tests/agent/test_tools.py::…registered_in_TOOLS` and `tests/agent/test_routing.py` (`TestToolCapability`, `TestShouldContinueMandatoryGate`)

### Implementation for User Story 4

- [x] T049 [US4] `RememberArgs` (`content` `max_length=2000`, `_not_blank`, injected `tool_call_id`), `_remember_impl` (payload `{text, kind:"memory", tenant, owner, created_at}` — `created_at` = `datetime.now(UTC).isoformat()`), `remember` (`_ctx_or_refuse(config, "write_memory")` → `idempotent(...)`) in `app/agent/tools.py`; `"remember": "mutating"` in `TOOL_CAPABILITIES`
- [x] T050 [US4] Automatic recall — `_memory_hits` (`Policy.lower(ctx, "memories")`, no re-rank), `gather_context` (documents + memories under one continuous numbering, `("", [])` on missing ctx), `recall_memories` — in `app/agent/tools.py`

**Checkpoint**: US4 independent of US5; retention is part of the recall filter, deletion is US5.

---

## Phase 7: User Story 5 — Memories expire and can be deleted, with a record that it happened (Priority: P2)

**Goal**: Retention at recall; operator-only deletion with exactly one selector and an audit trail.

**Independent Test**: Tier 1 `tests/agent/test_memory.py`; quickstart *Delete a memory*.

### Tests for User Story 5

- [x] T051 [P] [US5] Refuses without ctx; refuses with no/both selectors; always scopes to `ctx["tenant"]`; defaults to the caller's own principal; `target_principal` overrides within the tenant; `memory_id` uses `HasIdCondition`; `older_than_days` uses a range; counts before deleting with the same filter; records `deleted` / `refused` outcomes — `tests/agent/test_memory.py`

### Implementation for User Story 5

- [x] T052 [US5] `delete_memories(ctx, *, memory_id, older_than_days, target_principal)` — exactly one selector, tenant-scoped, count-then-delete, structured log `memory_deleted`, counter `agent_memory_deletion_total{outcome}` with no tenant/principal labels — in `app/agent/memory.py`
- [x] T053 [P] [US5] Retention at recall — `DatetimeRange(gte=now − MEMORY_RETENTION_DAYS)` ANDed onto the memories filter in `app/core/security.py::TenantIsolationPolicy.lower`

### Open follow-ups for User Story 5 (not built) — **A3**

- [ ] T054 [P] [US5] **A3 — write the failing test first**: new `tests/scripts/test_delete_memories.py` (hermetic; patch `app.agent.memory.delete_memories`) asserting a new `scripts/delete_memories.py` CLI (a) requires `--tenant` and `--principal` (the operator's ctx) and exactly one of `--memory-id` / `--older-than-days`, (b) accepts an optional `--target-principal`, (c) prints the count, (d) exits non-zero and prints the `ValueError` message on a refused call, (e) never deletes without `--yes` after printing what *would* be removed (use `qdrant_store.count_by_filter`). Fails today because the script does not exist
- [ ] T055 [US5] **A3 — implement** `scripts/delete_memories.py` (same "fixed pipeline, not an agent turn" shape as `scripts/tool_call_dedup_sweep.py`; calls `app.agent.memory.delete_memories` directly, never the agent loop) and a `memory-delete` Makefile target with a `##` help comment; document how an operator authenticates a data-subject request in README (the function does not authenticate `target_principal` entitlement — say so)
- [ ] T056 [US5] **A3 — decide on a physical expiry sweep**: retention is enforced only at recall, so expired points are never removed. Decide whether to add an operator-only `sweep_expired_memories()` (cross-tenant by design, so it must live in `scripts/`, not in the agent) that deletes every memory with `created_at < now − MEMORY_RETENTION_DAYS`; if yes, failing test first in `tests/scripts/`, counters via `agent_memory_deletion_total`, and an alert only if a sweep failure could leave a human unaware of retained personal data (Principle V, plan A6). Record the decision in research.md R11
- [ ] T057 [P] [US5] **Close the count-then-delete race (optional)**: if Qdrant's delete can return the affected count (verify against `qdrant-client==1.19.0` source or a real run — do not assume), remove the separate `count_by_filter` call in `app/agent/memory.py::delete_memories`; otherwise keep the disclosed accepted race

**Checkpoint**: US5 is *operable* only after T054–T055; today it is a library function.

---

## Phase 8: User Story 6 — Writes to the shared knowledge base carry their tenant (Priority: P2)

**Goal**: Every stored record carries its tenant; unowned content is refused, never stored.

**Independent Test**: Tier 1 `test_ingestor.py`, `test_tools.py::TestAddNoteImpl`; quickstart step 2.

### Tests for User Story 6

- [x] T058 [P] [US6] Ingestion refuses without ctx and counts the reason; payload carries `tenant` and `ingested_by` in `tests/ingestion/test_ingestor.py`
- [x] T059 [P] [US6] `add_note` argument validation (`title ≤ 200`, `content ≤ 4000`, `topic ∈ {langgraph, qdrant, company}`, blank rejected) and the stored payload in `tests/agent/test_tools.py` (`TestAddNoteArgsValidation`, `TestAddNoteImpl`)

### Implementation for User Story 6

- [x] T060 [P] [US6] `ingest_text` — `valid_ctx` gate raising `IngestRefused`, payload `{text, parent_id, parent_text, title, source, ingested_by, kind:"document", tenant}` — in `app/ingestion/ingestor.py`
- [x] T061 [P] [US6] `add_note` / `_add_note_impl` (payload `{text, topic, title, kind:"document", tenant}`, `uuid5(tool_call_id)` id, `_ctx_or_refuse(config, "write_note")`, `idempotent`) in `app/agent/tools.py`; `"add_note": "mutating"` in `TOOL_CAPABILITIES`
- [x] T062 [P] [US6] `POST /ingest/upload` takes `ctx = Depends(get_ctx)` (identity headers required) and per-file error reporting in `app/api/main.py`

### Open follow-ups for User Story 6 (not built)

- [ ] T063 [US6] **Record who wrote a note**: failing test first in `tests/agent/test_tools.py::TestAddNoteImpl` asserting the stored payload contains `added_by == ctx["principal"]`; then add `"added_by": ctx["principal"]` to the payload in `app/agent/tools.py::_add_note_impl`. Existing notes lack the field and stay valid (no filter reads it). This is attribution only — it does not add per-person authorization (spec *Known gaps*)

**Checkpoint**: US6 independently verifiable at the hermetic tier.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [x] T064 [P] Pattern entries with their motivating bugs — patterns 15, 17, 18, 19, 21, 22, 33, 49 — in `GRAPH_PATTERNS.md`; isolation and memory described in `README.md`
- [ ] T065 [P] **Disclose every gap in the project docs now (docs-only, no code)** — Constitution Principle VIII requires known limitations in the README *Roadmap* or *Extending Further*, and none of these is there today except "Real authentication": add to README *Roadmap* and `GRAPH_PATTERNS.md` *Extending Further* one entry each for **B2** (conversation ownership unchecked on send/resume/cancel; reproduced at graph level), **A5** (shipped proxy provides no authentication), **A3** (memory deletion has no entry point; retention is read-time only), **A4** (real-backend isolation proof covers only documents and the cache's tenant axis), **E1** (ops domain global and ungoverned), and the shared-tenant Telegram identity. Land this PR before any fix so the gaps are visible while they are open
- [x] T066 [P] Ran `/speckit-analyze` (read-only) over `spec.md`, `plan.md`, `tasks.md` on 2026-10-02 and reconciled what it found — see this feature's `checklists/requirements.md` *Validation iterations* for the findings, the corrections made, and the items deliberately left for a decision (requirement-id traceability tags; `promtool check rules` in CI)
- [ ] T067 Run `specs/002-tenant-isolation-and-memory/quickstart.md` Tier 2 and Tier 3 on a machine with Docker and a native Ollama (Tier 1 was run on 2026-10-02: 371 passed) and record the result in the PR that closes the open follow-ups; do **not** cite Tier 2 as isolation evidence until T022 lands

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup → Foundational → stories.** Foundational blocks every story.
- **US1, US2, US3** (P1) need only Phase 2 and are mutually independent.
- **US4** (P2) needs US2's `_ctx_or_refuse` and feature 003's approval gate (already built).
- **US5** (P2) needs US4's memory payload (`owner`, `created_at`).
- **US6** (P2) needs Phase 2 only.
- **Polish** last — except **T065**, which should land first.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T065, T032 | README, `GRAPH_PATTERNS.md`, `infra/README.md`, `Caddyfile` comments | docs-only; do first |
| 2 | T040–T043, T045 (B2 core) | `app/api/main.py`, `app/agent/sessions.py`, `app/job_queue/agent_worker.py`, tests | test-first; decision T041 gates it; may split API vs worker |
| 3 | T044 | Telegram regression test | after PR 2 |
| 4 | T022–T023 (A4) | new integration test, `app/core/security.py` comment | needs Docker; independent of PR 2 |
| 5 | T054–T055 (A3) | `scripts/delete_memories.py`, Makefile, tests | independent |
| 6 | T063 | `app/agent/tools.py`, `tests/agent/test_tools.py` | independent, tiny |
| 7 | T033 (E1) | `app/api/main.py` and/or the constitution | needs a policy decision first |
| 8 | T034, T046 (FR-003, FR-012 tests) | `tests/api/test_api.py` | tests only; hermetic; independent |
| — | T024, T025, T056, T057 | small / optional | bundle opportunistically |

PRs 1, 4, 5, 6 are mutually independent and can run in parallel; PR 2 is the one that changes
behavior users can observe.

### Parallel opportunities

- All Setup/Foundational [P] tasks; after Phase 2, US1/US2/US3 in parallel.
- Within each story every test task is [P]; implementation tasks on different files are [P].

## Parallel Example: User Story 1

```bash
# Tests together (different files):
Task: "T010 Filters in tests/core/test_security.py"
Task: "T012 query_employees in tests/agent/test_sql_store.py"
Task: "T014 MCP in tests/mcp/test_mcp_server.py"
# Implementation together:
Task: "T016 _document_hits in app/agent/tools.py"
Task: "T017 query_employees in app/agent/sql_store.py"
Task: "T019 Cache scope in app/retrieval/semantic_cache.py"
```

## Implementation Strategy

### As-built order (what happened)

Identity and policy first (patterns 17, 19), then Qdrant scoping and memory (18), the fixed-tool
structured store (21), the cache (22), retention/deletion (33), and the domain axis on sessions
(49). The history shows each layer added *after* the previous one revealed a gap — which is also why
the conversation checkpoint, added earlier for durability (feature 001), was never given an owner.

### Closing the open follow-ups (what to do next)

1. **PR 1 now** — disclosure costs nothing and turns an unknown risk into a tracked one.
2. **PR 2 (B2)**, test-first, after deciding T041. It is the only item that lets one caller affect
   another's data.
3. **PR 4 (A4)** — converts Principle I from believed to proven for reads and verifies the two
   comment-only Qdrant behaviors; it may *change the spec* if (a) in T023 is false.
4. **PRs 5–7** — operability, attribution, policy.
5. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

User Story 1 + 2 (T001–T031) is the minimum isolation story; **US3** is required to honor owner
scoping, and **B2 must be closed before the system is exposed to mutually untrusting tenants** —
the constitution makes Principle I non-negotiable and B2 is a literal violation of it.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (371 passed) was re-run on 2026-10-02.
- Tier 3 and the real-backend claims are **not** verified by this batch.
- Features 001 (the pipeline) and 003 (the approval gate and exactly-once writes) own behavior this
  feature merely relies on; tasks touching them say so and do not restate them.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks.
