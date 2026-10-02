---

description: "Task list for feature 005 — Multi-Domain Composition (retrospective)"
---

# Tasks: Multi-Domain Composition (Support, Ops, Sales on One Graph)

**Input**: Design documents from `/specs/005-multi-domain-composition/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle I is NON-NEGOTIABLE and Principle VII requires a regression test for every bug fix. B7 — a customer
reading another customer's ticket — was *missed by the existing tests*, which assert statement shape against fake cursors and never
ask "whose row is this?"; the open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first** (CLAUDE.md working
  rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B7** a ticket is addressable by number by any principal of the
  tenant · **B8** one failing follow-up aborts the sweep · **A1** two docstrings (and a tool description) describe tool sets that no longer
  exist · **A2** the sweep can repeat a draft after a crash · **A3** the sweep covers only the default tenant · **A4** the ops thresholds are
  a copy of the alert rules, unchecked · **A5** no ops "default-only tools absent" test, `ops_investigate.py` untested · **A6** "idempotent"
  is loose wording · **A7** no length bounds on free-form domain-tool arguments; one categorical argument is free-form.
- **B7 is a Principle I defect (owner scoping of personal data, within a tenant); no write happens without approval in any case here.**
- Tasks needing Docker say `integration`. Paths are repo-relative.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US6 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] The three domains' tables — `support_tickets` in `postgres-init/07-support-tickets.sql`, `crm_leads` + `crm_followups` in `postgres-init/08-crm.sql`, `ops_incidents` (no tenant) in `postgres-init/10-ops-incidents.sql` *(FR-013, FR-018, FR-019)*
- [x] T002 [P] Row-level keys and appends as rows — nullable `tool_call_id TEXT UNIQUE` columns in `postgres-init/14-tool-call-id-columns.sql`; `support_ticket_comments` and `crm_lead_notes` (each with its own `tenant`) in `postgres-init/15-append-notes-as-rows.sql`; the since-dropped `notes` column in `postgres-init/09-support-ticket-notes.sql` *(FR-013, FR-016)*
- [x] T003 [P] Per-domain worker services and run targets — `agent-worker-support`/`-ops`/`-sales` in `docker-compose.yml`; `agent-worker-*` and `telegram-support`/`telegram-sales` in `Makefile` *(FR-004, FR-027)*
- [x] T004 [P] Domain-tagged catalogs — `domains: [support|ops|sales]` in `skills/support-tier1-triage/SKILL.md`, `skills/support-log-triage/SKILL.md`, `skills/ops-incident-response/SKILL.md`, `skills/vendor-incident-postmortem/SKILL.md`, `skills/sales-lead-qualification/SKILL.md`, `skills/deal-economics/SKILL.md`; sub-assistants `subagents/ticket-researcher/AGENT.md`, `subagents/metrics-researcher/AGENT.md`, `subagents/vendor-history-researcher/AGENT.md`, `subagents/lead-researcher/AGENT.md` *(FR-011)*
- [x] T005 [P] Red-team configs that found the prompt-level leaks — `promptfoo/ops-redteam.yaml`, `promptfoo/sales-redteam.yaml` (the per-domain behavior suites are `promptfoo/ops.yaml`, `promptfoo/sales.yaml`, `promptfoo/support.yaml`) *(US5)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The seam every domain plugs into.

- [x] T006 `AgentManifest` (frozen), the `DomainPlugin` protocol and `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` in `app/agent/manifest.py` *(FR-001, FR-002)*
- [x] T007 `build_graph(manifest, domain)` — one topology, the capability map bound once, the manifest stashed on the compiled graph — in `app/agent/graph_build.py` *(FR-001, FR-005, FR-006)*
- [x] T008 The registry and `resolve_domain` (unknown name → `ValueError` listing the valid names) in `app/domains/registry.py` *(FR-003)*
- [x] T009 [P] `ActionAllowlistPolicy` (`permit` = in the set **and** a valid ctx; `lower` raises) in `app/domains/policy.py` *(FR-010)*
- [x] T010 [P] The team-channel sink — never raises; counted by `agent_team_channel_notify_total` — in `app/domains/notify.py`, tested in `tests/domains/test_notify.py` *(FR-010)*
- [x] T011 [P] `skill_tools_first`, `make_skill_tools` in `app/agent/tools.py`; the per-domain delegation tool (absent when no bundled sub-assistant declares the domain) in `app/agent/subagent_domain_tools.py` *(FR-011, FR-012)*
- [x] T012 `init_graph_async(manifest, domain)` and thread seeding with the *domain's* prompt (`graph.manifest.system_prompt`) in `app/agent/runtime.py` *(FR-004, FR-006)*

**Checkpoint**: Foundation ready — a domain can be registered, built and seeded.

---

## Phase 3: User Story 1 — A new use case is configuration plus code in a fixed shape, never a fork (Priority: P1) 🎯 MVP

**Goal**: One pipeline serves every assistant; the default assistant is itself a domain.

**Independent Test**: Build the graph with the default pair and with a toy domain; resolve each name and an unknown one.

### Tests for User Story 1

- [x] T013 [P] [US1] The default pair reproduces the original assistant; a toy "widget" domain sees only its own tool, its write tool pauses and runs once approved, the default domain's declarations are unaffected, the leak check uses its prompt — in `tests/agent/test_manifest.py` *(FR-001, FR-002, FR-006, SC-001)*
- [x] T014 [P] [US1] Each name resolves to its manifest; an unknown name raises listing the valid ones — in `tests/domains/test_registry.py` *(FR-003, SC-008)*
- [x] T015 [P] [US1] An unknown `X-Domain` is a 422 and a known one passes through in `tests/api/test_api.py` (`TestGetDomain`) *(FR-003)*

### Implementation for User Story 1

- [x] T016 [US1] `AGENT_DOMAIN` resolved once at process start (worker, chat app) and `X-Domain` validated per request — `app/core/config.py`, `get_domain` in `app/api/main.py` (feature 004) *(FR-003, FR-004)*

**Checkpoint**: US1 delivers the seam on its own.

---

## Phase 4: User Story 2 — A domain's tool list is its sandbox (Priority: P1)

**Goal**: Each domain exposes only its own tools; every write tool is gated and exactly-once.

**Independent Test**: List each domain's tools; confirm the excluded tools are absent; run the contract test.

### Tests for User Story 2

- [x] T017 [P] [US2] Support and sales expose none of the default assistant's calculator/note/memory/employee tools; the sandbox tools are a fixed set declared outward — `TestSandboxing` in `tests/domains/support/test_domain.py` and `tests/domains/sales/test_domain.py` *(FR-005, FR-007, FR-009, FR-010, SC-003)*
- [x] T018 [P] [US2] Each write tool pauses for approval and runs once approved; read-only tools never pause — the per-tool tests in `tests/domains/support/test_domain.py`, `tests/domains/ops/test_domain.py`, `tests/domains/sales/test_domain.py` *(FR-010)*
- [x] T019 [P] [US2] Every write tool of every domain refuses without a valid ctx and routes through `idempotent()` with its call id and name (27 tools × 2 checks) in `tests/domains/test_write_tools_contract.py` *(FR-010, SC-002)*
- [x] T020 [P] [US2] A domain's delegation tool is not the default assistant's and offers only its own sub-assistants — `TestDomainScopedSubagent` in `tests/domains/*/test_domain.py` *(FR-011, SC-004)*

### Implementation for User Story 2

- [x] T021 [P] [US2] The support domain — prompt, 15 tools, tier map, `SUPPORT_POLICY` — in `app/domains/support/domain.py` and `app/domains/support/tools.py` *(FR-007)*
- [x] T022 [P] [US2] The ops domain — prompt (with the sandbox-boundary, scope and anti-disclosure paragraphs), 14 tools, `OPS_POLICY` — in `app/domains/ops/domain.py` and `app/domains/ops/tools.py` *(FR-008)*
- [x] T023 [P] [US2] The sales domain — prompt (with the anti-disclosure paragraph), 16 tools, `SALES_POLICY` — in `app/domains/sales/domain.py` and `app/domains/sales/tools.py` *(FR-009)*

### Open follow-ups for User Story 2 (not built) — **A5, A7**

- [ ] T024 [P] [US2] **A5 — write the ops negative test**: in `tests/domains/ops/test_domain.py` add `test_ecorp_only_tools_are_absent` (copy the support one) asserting `calculator`, `add_note`, `remember`, `query_employees` and `search_docs` are not in the ops manifest's `allowed_tools` *(FR-008, SC-003)*
- [ ] T025 [US2] **A7 — write the failing test first**: extend `tests/domains/test_write_tools_contract.py` (or add `tests/domains/test_tool_argument_bounds.py`) with a registry-wide test that every `str` property of every domain-module tool's argument schema has a `maxLength` (allow-list deliberate exceptions with a reason). Fails today for 41 fields *(FR-029, SC-010)*
- [ ] T026 [US2] **A7 — fix**: declare `max_length` from named constants (as `app/agent/tools.py` does for notes and memories) on every free-form field in `app/domains/support/tools.py`, `app/domains/ops/tools.py` and `app/domains/sales/tools.py`; make `list_recent_incidents`'s `status` a closed enum. The numbers are a product decision (note them in the PR). Split the PR per domain if it exceeds the line budget *(FR-029)*

**Checkpoint**: after T024–T026 SC-003 and SC-010 hold for every domain.

---

## Phase 5: User Story 3 — Each domain keeps its own data in its own tenant-scoped store (Priority: P1)

**Goal**: Tenant on every statement; owner scoping where the data is personal.

**Independent Test**: Read every statement; try to read, escalate and comment on a ticket as another customer of the same tenant.

### Tests for User Story 3

- [x] T027 [P] [US3] Store statements carry the tenant and are parameterized; upsert and lost-lead closing; append rows are keyed — in `tests/domains/support/test_store.py`, `tests/domains/sales/test_store.py`, `tests/domains/ops/test_store.py` (fake cursors — statement shape only) *(FR-013, FR-016, FR-017)*

### Implementation for User Story 3

- [x] T028 [P] [US3] The support store — create (keyed), get (with `STRING_AGG` comments), the owner-scoped listing, escalate, add comment (insert + `updated_at` in one transaction) — in `app/domains/support/store.py` *(FR-013, FR-014)*
- [x] T029 [P] [US3] The sales store — find-or-create lead (unique upsert + note row), follow-ups, due list, lost-lead closing (one transaction), notes — in `app/domains/sales/store.py` *(FR-016, FR-017)*
- [x] T030 [P] [US3] The ops store — keyed incident insert, list, resolve; no tenant — in `app/domains/ops/store.py` *(FR-018)*
- [x] T031 [P] [US3] Each tool's `_ctx_or_refuse` (valid ctx **and** the allowlist policy) before any store call — in `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py` *(FR-010)*

### Open follow-ups for User Story 3 (not built) — **B7**

- [ ] T032 [US3] **B7 — write the failing test first**: in `tests/domains/support/test_store.py` (and a tool-level case in `tests/domains/support/test_domain.py`) add a test with two customers in one tenant asserting that, for the second, `get_ticket`/`check_ticket_status`, `escalate_ticket` and `add_comment` find nothing while the requester still succeeds. Fails today: all three succeed for the other customer (reproduced — quickstart *Scenario B7*) *(FR-015, SC-005)*
- [ ] T033 [US3] **B7 — decide, then fix**: decide whether an authorized support agent (a claim in `ctx["claims"]`) may see any ticket in the tenant; then add the requester condition to `get_ticket`, `escalate_ticket` and `add_comment` in `app/domains/support/store.py` (and pass the principal from `app/domains/support/tools.py`), update `specs/…/contracts/domain-tool-surfaces.md`, and update the `list_tickets_for_requester` docstring that justifies the old asymmetry *(FR-015)*
- [ ] T034 [US3] **Prove the scoping against a real Postgres (`integration`; Docker)**: new `tests/integration/test_domain_stores_real_postgres.py` using `tests/containers.py::ensure_postgres()` — two tenants and two customers: a ticket, a lead, a follow-up and an incident; assert tenant and owner scoping, the unique `(tenant, contact)` upsert, the `tool_call_id` `ON CONFLICT` no-op and the lost-lead transaction. The only way to turn the fake-cursor tests' claims into proof (Principle VII known gap) *(FR-013, FR-014, FR-016)*

**Checkpoint**: US3 meets SC-005 after T032–T033.

---

## Phase 6: User Story 4 — Scheduled work is a fixed pipeline, never an agent turn (Priority: P2)

**Goal**: Cron jobs never enter the tool loop; a failure on one item never blocks the rest.

**Independent Test**: Run each job against a stand-in model and store.

### Tests for User Story 4

- [x] T035 [P] [US4] The digest flags, summarizes, posts once and records usage only when tokens are reported — `tests/scripts/test_ops_digest.py` *(FR-021, SC-006)*
- [x] T036 [P] [US4] The sweep drafts, posts and marks each due follow-up; nothing due → no model call; usage recorded — `tests/scripts/test_followup_sweep.py` *(FR-022)*
- [x] T037 [P] [US4] Threshold comparison (strictly greater), `None` readings, query failure, empty vector — `tests/domains/ops/test_metrics_client.py` *(FR-021)*

### Implementation for User Story 4

- [x] T038 [P] [US4] The ops digest — fixed pipeline under the `ops-cron` principal — in `scripts/ops_digest.py` *(FR-020, FR-021)*
- [x] T039 [P] [US4] The follow-up sweep — due items, one drafted nudge each, post for review, mark done, `sales-followup-cron` principal — in `scripts/followup_sweep.py` *(FR-020, FR-022)*
- [x] T040 [P] [US4] The ad-hoc investigation — one-shot ops graph, local identity, the disclosed no-resume limit — in `scripts/ops_investigate.py` *(FR-025)*
- [x] T041 [P] [US4] The Prometheus client and the threshold checks mirroring the alert rules in `app/domains/ops/metrics_client.py` *(FR-021)*

### Open follow-ups for User Story 4 (not built) — **B8, A2, A3, A4, A5**

- [ ] T042 [US4] **B8 — write the failing test first**: in `tests/scripts/test_followup_sweep.py` add a test with three due items and a model that raises for the second, asserting the first and third are drafted, posted and marked done, the second stays pending, and the run reports failure. Fails today: the sweep raises after the first (reproduced — quickstart *Scenario B8*) *(FR-023, SC-007)*
- [ ] T043 [US4] **B8 — fix**: guard each item in `run_followup_sweep` in `scripts/followup_sweep.py` (`except Exception  # noqa: BLE001 - one failing lead must not block the rest`), log with the item id, count it, continue, and exit non-zero at the end if any failed so the scheduler still sees it *(FR-023)*
- [ ] T044 [P] [US4] **A3 — sweep every tenant with due follow-ups**: add a store function returning the distinct tenants with due pending follow-ups to `app/domains/sales/store.py`, iterate them in `scripts/followup_sweep.py` (reusing the B8 guard), with a test in `tests/scripts/test_followup_sweep.py` and `tests/domains/sales/test_store.py` *(FR-024)*
- [ ] T045 [P] [US4] **A4 — pin the thresholds to the alert rules**: a test in `tests/domains/ops/test_metrics_client.py` that parses `observability/prometheus/alerts.yml` and asserts every `CHECKS` expression and threshold appears in it (and the reverse for the rules the digest mirrors) *(FR-026)*
- [ ] T046 [P] [US4] **A5 — test the ad-hoc script**: new `tests/scripts/test_ops_investigate.py` with a stub graph — a read-only answer is returned; a paused run returns `(no answer produced)` or the empty text; the thread id and local identity are set *(FR-025)*
- [ ] T047 [US4] **A2 — decide**: keep post-then-mark and disclose the repeat-after-crash window (a duplicate human-reviewed draft), or mark first (a crash then drops a nudge). Record the decision in `specs/005-multi-domain-composition/research.md`; either way note it in the docstring of `scripts/followup_sweep.py` *(FR-022)*

**Checkpoint**: after T042–T043 FR-023 and SC-007 hold; after T044 FR-024.

---

## Phase 7: User Story 5 — Each example domain delivers a complete use case (Priority: P2)

**Goal**: Support, ops and sales each deliver their headline flow end to end.

**Independent Test**: For each domain, walk its headline flow with the model stubbed.

### Tests for User Story 5

- [x] T048 [P] [US5] Support: ticket create/status/list/comment, escalation notifies the team channel, the external-page and sandbox tools pause and run once approved — in `tests/domains/support/test_domain.py` *(FR-007)*
- [x] T049 [P] [US5] Ops: metrics read-only, incident log/list/resolve, vendor page and team post pause and run once approved — in `tests/domains/ops/test_domain.py` *(FR-008)*
- [x] T050 [P] [US5] Sales: interaction logging, pending queue, lost-lead closing, hand-off notifies the team channel, website enrichment pauses and runs once approved — in `tests/domains/sales/test_domain.py` *(FR-009)*

### Implementation for User Story 5

- [x] T051 [P] [US5] The typed tools for each flow — `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py` *(FR-007, FR-008, FR-009)*
- [x] T052 [P] [US5] The prompt-level defenses (sandbox boundary, scope discipline, anti-disclosure) and their honest ceiling — in `app/domains/ops/domain.py` and `app/domains/sales/domain.py` *(US5)*

**Checkpoint**: US5 is exercised hermetically per domain; the live behavior depends on the model.

---

## Phase 8: User Story 6 — Adding or changing a domain is a recipe, and the recipe is checked (Priority: P3)

**Goal**: A documented shape and tests that catch a skipped step.

**Independent Test**: Add a toy domain in a test; run the contract test.

### Implementation for User Story 6

- [x] T053 [P] [US6] The README *Example domains* section — the domain table, the cron-script rationale, the ad-hoc caveat, how a process picks a domain — in `README.md` *(FR-027)*
- [x] T054 [P] [US6] The side-effect-tool checklist loaded when tools, stores or init scripts change, in `.claude/rules/side-effect-tools.md` *(FR-027)*

### Open follow-ups for User Story 6 (not built) — **A1, A6**

- [ ] T055 [US6] **A1 — correct the stale texts (docs-only, do first)**: the `ops/domain.py` docstring ("only domain wired to OpenSandbox") in `app/domains/ops/domain.py`; the `support/domain.py` docstring ("exactly those four plus five ticket tools") in `app/domains/support/domain.py`; and `handoff_to_human`'s description ("mark a lead 'hot'" — the status is `handed_off`) in `app/domains/sales/tools.py`. Constitution Governance: a conflicting document MUST be corrected *(FR-028, SC-009)*
- [ ] T056 [P] [US6] **A6 — reword**: "idempotent" → "safe to re-run" in the `scripts/ops_digest.py` docstring *(FR-021)*

**Checkpoint**: after T055 SC-009 holds.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T057 [P] **Disclose every open gap in the project docs now (docs-only)** — Principle VIII: add one entry each for **B7**, **B8** and **A1**–**A7** to `GRAPH_PATTERNS.md` *Extending Further* and a short list to the README *Roadmap*, each stating how it was established (reproduced vs. read) and that no unreviewed write results from any of them. Land this before any fix
- [ ] T058 Re-run `specs/005-multi-domain-composition/quickstart.md` Tiers 1 and 3 and both scenarios after the fixes; delete each resolved row from plan.md *Complexity Tracking* and each resolved gap from spec.md *Known gaps*
- [ ] T059 [P] After B7 lands, update pattern 47 in `GRAPH_PATTERNS.md` (it describes the domains but not their scoping rules) and the README *Example domains* support row

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup → Foundational → stories.** Foundational blocks every story.
- **US1, US2, US3** (P1) need only Phase 2 and are mutually independent; US2's contract test reuses the registry from US1.
- **US4** (P2) needs US3's stores (the sweep reads the sales store) and `app/domains/notify.py` (Phase 2).
- **US5** (P2) needs US2 and US3. **US6** (P3) is independent of the others at the code level.
- **Polish** last — except **T057** and **T055**, which land first.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T057, T055, T056 | `GRAPH_PATTERNS.md`, README, three docstrings/descriptions | docs-only; do first |
| 2 | T032–T033 (B7) | `support/store.py`, `support/tools.py`, two test files | test-first; needs the authorized-agent decision |
| 3 | T042–T043 (B8) | `scripts/followup_sweep.py`, one test file | test-first; small |
| 4 | T044 (A3) | `sales/store.py`, `followup_sweep.py`, two test files | after PR 3 (shares the guard) |
| 5 | T024, T045, T046 (A5, A4) | three test files | tests only; independent |
| 6 | T025–T026 (A7) | three `tools.py`, one test file | decision on the bounds; split per domain if large |
| 7 | T034 | new integration test | Docker; independent; the real-database proof |
| — | T047 (A2) | decision | bundle with PR 3 |

PRs 2, 3, 5, 7 are mutually independent; run them in parallel.

### Parallel opportunities

- Setup T001–T005 and Foundational T009–T011 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 4

```bash
# Tests together (different files):
Task: "T035 Digest in tests/scripts/test_ops_digest.py"
Task: "T036 Sweep in tests/scripts/test_followup_sweep.py"
Task: "T037 Metrics client in tests/domains/ops/test_metrics_client.py"
# Implementation together:
Task: "T038 Digest in scripts/ops_digest.py"
Task: "T039 Sweep in scripts/followup_sweep.py"
Task: "T041 Metrics client in app/domains/ops/metrics_client.py"
```

## Implementation Strategy

### As-built order (what happened)

The manifest/plugin seam was proved first with a toy domain (pattern 23) → three real domains followed on 2026-08-30 (support, ops, sales, each in the
same three-file shape) → the chat-app channel gave them a customer-facing front door → skills and sub-assistants became domain-tagged → the sandbox and
crawl tools were wired into the domains (so two early docstrings went stale) → red-teaming added prompt-level defenses → the duplicate-write audits
turned appends into keyed rows → `AGENT_DOMAIN` and per-domain streams let one API front every pool. B7 sits in a decision taken *before* the chat-app
channel made every customer a principal of one tenant; B8 in a loop never exercised with a failing item.

### Closing the open follow-ups (what to do next)

1. **PR 1 (docs)** now — two docstrings are wrong and the disclosures make the open gaps visible.
2. **PR 2 (B7)** — the only defect that crosses customers; decide the authorized-agent question first.
3. **PRs 3–4 (B8, A3)** — make the sweep robust and complete.
4. **PRs 5–7** — turn the remaining audits into tests (thresholds, ops negative test, argument bounds) and, with Docker, prove the stores' scoping
   against a real Postgres.
5. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 (T001–T023) is the minimum that gives one pipeline and per-domain sandboxes; **US3** makes each domain's data tenant-scoped. B7 does not
weaken tenant isolation or the approval gate, so the system is *safe* to run as it stands — but a customer-facing support deployment should treat B7
as a blocker, because every customer shares one tenant there.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (214 passed) was re-run on 2026-10-02, plus the reproduction scenarios.
- Tier 2/3 and the real-database scoping claims are **not** verified by this batch.
- Features 001 (the turn), 002 (identity, ownership, the global ops data), 003 (the gate and the write rules), 004 (routing to a pool), 007 (the
  catalogs) and 009 (the sandbox and crawl tools) own behavior this feature relies on.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks.
