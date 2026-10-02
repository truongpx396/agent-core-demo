---

description: "Task list for feature 007 — Skills and Subagents (retrospective)"
---

# Tasks: Skills and Subagents

**Input**: Design documents from `/specs/007-skills-and-subagents/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle VII requires a regression test for every bug fix. All four defects (B13–B16) were *missed by the existing tests*: the loader tests use `tmp_path`, the delegation tests use
fakes and assert the outcome counter but not the ledger or the memory, and nothing loads the shipped catalogs or inspects the image. The open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first** (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B13** the cached nested graph retains every run · **B14** a timed-out or failed run's tokens are never recorded · **B15** two Ecorp-only
  skills are offered in every product · **B16** the container images contain neither catalog · **A1** unbounded `query`/`name`/`task` · **A2** no deploy path builds the index; the degrade is unmetered · **A3** a
  running process does not see catalog edits · **A4** tags, tool names and aliases unvalidated · **A5** crude required-tool detection and a dead branch · **A6** a specialist's answer re-enters unframed · **A7** no
  test pins the shipped catalogs · **A8** configuration and comment drift.
- **No defect here weakens tenant isolation or approval**: a specialist can only ever read, and that guarantee is structurally enforced and well tested. B14 is an *accounting* defect; B15 is a *correctness*
  defect; B16 means the feature is **absent** in containers.
- Tasks needing Docker say `docker`. Paths are repo-relative. A path that does not exist yet is a file the task creates.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US5 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] The catalog content — 8 skills in `skills/*/SKILL.md` (6 tagged to one product, **2 untagged — B15**) and 5 specialists in `subagents/*/AGENT.md` (support 1, sales 1, ops 2, and the Ecorp-only `researcher`) *(FR-001, FR-010)*
- [x] T002 [P] Settings `skills_dir`, `skills_collection`, `skills_search_top_k` (1), `subagents_dir`, `subagent_timeout_seconds` (45), `max_subagent_cost_usd_per_run` (0.15) in `app/core/config.py` (**no `.env.example` entries — A8**) *(FR-017)*
- [x] T003 [P] Counters and histogram `agent_use_skill_without_search_total`, `agent_skipped_required_tool_total`, `agent_subagent_run_total{subagent,outcome}`, `agent_subagent_duration_seconds{subagent}` in `app/core/metrics.py` *(FR-008, FR-009, FR-020)*
- [x] T004 [P] The `make index-skills` target, which runs `scripts/index_skills.py`, in `Makefile` (its only automated caller is `restart-all`) *(FR-004)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The pieces every story uses.

- [x] T005 [P] The skill loader — `SkillRecord`, `_parse_skill_file`, `load_skills`, the lazy `get_skills`, `reload_skills` — in `app/agent/skills.py` *(FR-001, FR-002)*
- [x] T006 [P] The subagent loader — `SubagentRecord`, `_parse_subagent_file`, `load_subagents`, `get_subagents`, `reload_subagents` — in `app/agent/subagents.py` *(FR-010, FR-002)*
- [x] T007 [P] The optional `collection` parameter on `ensure_collection`, `upsert` and `hybrid_search` so skills reuse the fusion logic — in `app/retrieval/qdrant_store.py` *(FR-004)*
- [x] T008 The indexing script (embeds `name: description` only, recreates the collection, random point ids) in `scripts/index_skills.py` *(FR-003, FR-004)*
- [x] T009 [P] `State.subagent_spend`, its `_concat_or_reset` reducer, the per-turn reset in `validate_input`, and the `MAX_SUBAGENT_ITERATIONS`/`MAX_SUBAGENT_TOKENS_PER_RUN` constants in `app/agent/graph.py` *(FR-017, FR-019)*
- [x] T010 `build_subagent_graph` (the leaner topology, five nodes dropped) and the shared `_assemble_shared_graph_parts` in `app/agent/graph_build_subagent.py` and `app/agent/graph.py` *(FR-017)*

**Checkpoint**: Foundation ready — catalogs load, the index can be built, a nested graph can be assembled and its spend can be folded.

---

## Phase 3: User Story 1 — The assistant finds and follows a packaged procedure for a task (Priority: P1) 🎯 MVP

**Goal**: Search by meaning, load one procedure, follow it; two guardrails against misuse.

**Independent Test**: Ask for a task matching a bundled procedure; confirm search, then load by exact name, then the named tools.

### Tests for User Story 1

- [x] T011 [P] [US1] Search and load — dedicated collection, name/description lines, no-hit and missing-collection messages, body from disk, unknown name, no ctx needed — in `tests/agent/test_tools.py` (`TestSkillSearch`, `TestUseSkill`) *(FR-003, FR-004, FR-005, FR-006)*
- [x] T012 [P] [US1] `use_skill` without a search is flagged, routed and retried without pausing — in `tests/agent/test_routing.py` (`TestUseSkillCalledWithoutSearch`, `TestShouldContinueUseSkillWithoutSearch`) and `tests/agent/test_graph_integration.py` (`TestUseSkillWithoutSearchGate`) *(FR-008)*
- [x] T013 [P] [US1] The required-tool reminder and the reactive correction — in `tests/agent/test_nodes.py` (`TestSkippedRequiredSandboxAfterSkill`, `test_retry_output_tells_the_model_the_skill_named_a_required_tool`) and `tests/agent/test_agent_node.py` (the two sandbox-reminder tests) *(FR-009)*
- [x] T014 [P] [US1] The skill tools lead the bound list; `run_subagent` joins the tier — in `tests/agent/test_tools.py` (`TestSkillToolsFirst`) *(FR-007)*
- [x] T015 [P] [US1] A real skill is found and followed on the real small model (`docker`, live) — in `tests/live/test_chat_ui.py` (`test_a_skill_is_found_and_followed`) *(SC-001)*

### Implementation for User Story 1

- [x] T016 [US1] `SkillSearchArgs`, `UseSkillArgs`, `_skill_visible_to_domain`, `make_skill_tools` (over-fetch, filter, truncate), `skill_tools_first` and the Ecorp pair, in `app/agent/tools.py` *(FR-003, FR-004, FR-005, FR-006, FR-007)*
- [x] T017 [P] [US1] The guardrails — the `use_skill_without_search` node and `_SKILL_REQUIRED_TOOL_MARKERS`/`_pending_skill_required_tool` in `app/agent/graph_skills.py`, `_use_skill_called_without_search` in `app/agent/graph_loop_guards.py`, `_skipped_required_sandbox_after_skill` in `app/agent/graph_output_guardrails.py`, and their routes in `app/agent/graph_routing.py` *(FR-008, FR-009)*

### Open follow-ups for User Story 1 (not built) — **A1, A5**

- [ ] T018 [US1] **A1 — write the failing test first**: in `tests/agent/test_tools.py` add tests that `SkillSearchArgs` rejects a `query` over the bound and `UseSkillArgs` rejects a `name` over the bound, and that every shipped skill name is under it. Fails today (no bound) *(FR-027)*
- [ ] T019 [US1] **A1 — fix**: `max_length` on `SkillSearchArgs.query` and `UseSkillArgs.name` in `app/agent/tools.py`, as named module constants (proposal: 500 and 100 characters — a skill name is at most ~30) *(FR-027)*
- [ ] T020 [US1] **A5 — write the characterization test first**: in `tests/agent/test_tools.py` (`TestUseSkill`) add a test that a skill whose body names `run_python_in_sandbox` is returned **exactly as written** (no appended text) and one for the `run_command_in_sandbox` reminder branch, so the dead branch is either pinned or provably unused *(FR-009)*
- [ ] T021 [US1] **A5 — fix**: make `_SKILL_REQUIRED_TOOL_MARKERS` in `app/agent/graph_skills.py` the single list of tools a skill may require; delete the second, unreferenced `run_command_in_sandbox` reminder in `app/agent/tools.py::_use_skill_impl` (no shipped skill triggers it), or keep it and add its name to the one list — decide, and say which in the commit *(FR-009)*

**Checkpoint**: US1 works as built; T018–T021 are hardening.

---

## Phase 4: User Story 2 — A procedure or specialist is offered only in the product it was written for (Priority: P1)

**Goal**: Tags are honoured in search, load, menus and tool construction.

**Independent Test**: In each product list the visible skills and the specialist menu; each sees only its own plus deliberately shared items.

### Tests for User Story 2

- [x] T022 [P] [US2] Skill visibility — untagged everywhere, tagged only to its products, a stale index hit dropped, a foreign skill hidden from search and refused by exact name — in `tests/agent/test_tools.py` (`TestSkillVisibleToDomain`, `TestFilterSkillHitsByDomain`, `TestMakeSkillTools`) *(FR-004, FR-005)*
- [x] T023 [P] [US2] Specialist declaration and menus — untagged is Ecorp-only, the domain filter, a domain with none gets no tool, independent enum types — in `tests/agent/test_tools.py` (`TestSubagentDeclaredForDomain`, `TestBuildSubagentRegistryDomainFilter`, `TestMakeDomainSubagentTool`) and `tests/domains/support/test_domain.py`, `tests/domains/ops/test_domain.py`, `tests/domains/sales/test_domain.py` (`TestDomainScopedSubagent`) *(FR-011, FR-013)*

### Implementation for User Story 2

- [x] T024 [US2] A per-product skill pair built by each product — `make_skill_tools("support")` and the like — in `app/domains/support/domain.py`, `app/domains/ops/domain.py`, `app/domains/sales/domain.py` *(FR-004, FR-005)*
- [x] T025 [P] [US2] `_subagent_declared_for_domain`, `_build_subagent_registry` in `app/agent/subagent_tools.py` and `make_domain_subagent_tool` in `app/agent/subagent_domain_tools.py` *(FR-011, FR-013)*

### Open follow-ups for User Story 2 (not built) — **B15, A4, A7**

- [ ] T026 [US2] **B15 / A4 / A7 — write the failing catalog-pinning tests first**: new `tests/agent/test_shipped_catalogs.py` loading the **real** `skills/` and `subagents/` folders and asserting (a) every shipped skill declares `domains`; (b) every `domains` value in either catalog is `ecorp` or a key of `app/domains/registry.py::DOMAINS`; (c) for each product, every skill visible to it names only tools that product has — with an **explicit, reviewed exception list** (today: `deal-economics` names `calculator` only as what it cannot do), because a bare name match false-positives (research R23); (d) every shipped specialist resolves with **no** dropped tool in each product it is declared for; (e) every specialist `model`, if set, is a `model_name` in `litellm-config.yaml`. (a) and (c) fail today — reproduced, quickstart *Scenario B15* *(FR-025, FR-028, SC-002, SC-010)*
- [ ] T027 [US2] **B15 — fix**: add `domains: [ecorp]` to `skills/onboarding-brief/SKILL.md` and `skills/expense-summary/SKILL.md`; T026 goes green. No re-index is needed (the index holds names, the tag lives on disk) but a running process needs a restart (A3) *(FR-025)*
- [ ] T028 [US2] **A4 — write the failing test first**: in `tests/agent/test_tools.py` (`TestResolveSubagentTools`) assert that dropping an unknown or non-read-only specialist tool increments a new counter labelled by specialist and reason. Fails today (log line only) *(FR-028)*
- [ ] T029 [US2] **A4 — fix**: add `agent_subagent_tool_dropped_total{subagent,reason}` to `app/core/metrics.py` and increment it in `_resolve_subagent_tools` in `app/agent/subagent_tools.py` *(FR-028)*

**Checkpoint**: US2 is *verified* against the shipped catalogs only after T026–T027.

---

## Phase 5: User Story 3 — The assistant hands a self-contained lookup to an isolated, read-only specialist (Priority: P1)

**Goal**: A nested, isolated, read-only run whose answer folds into the parent; never an approval.

**Independent Test**: Delegate in each product; confirm the sole human message is the task, the tools are read-only, no pause.

### Tests for User Story 3

- [x] T030 [P] [US3] Read-only resolution and the nested plugin — drops a mutating tool, an unknown tool, `run_subagent`; empty stays empty; every resolved tool reports `read_only` — in `tests/agent/test_tools.py` (`TestResolveSubagentTools`, `TestSubagentDomainPluginCapabilityFix`, `TestBuildSubagentGraphTopology`) *(FR-012, SC-003)*
- [x] T031 [P] [US3] The delegated run — no ctx refusal, unknown name, delegates and answers, the nested prompt and citation notice, the task as sole human message, step ceiling, no-progress budget message, auto-corrected uncited answer — and the tool's schema and spend `Command` — in `tests/agent/test_tools.py` (`TestRunSubagentImpl`, `TestRunSubagentTool`) *(FR-013, FR-015, FR-016, FR-017, FR-018)*
- [x] T032 [P] [US3] A delegation never pauses — in `tests/agent/test_routing.py` (`TestShouldContinueSubagent`) and each product's `TestDomainScopedSubagent::test_never_pauses_it_is_read_only` in `tests/domains/support/test_domain.py`, `tests/domains/ops/test_domain.py`, `tests/domains/sales/test_domain.py` *(FR-014)*
- [x] T033 [P] [US3] A real delegation returns a real answer on the real small model (`docker`, live) — in `tests/live/test_chat_ui.py` (`test_a_subagent_delegates_and_returns_a_real_answer`) *(SC-001, SC-004)*

### Implementation for User Story 3

- [x] T034 [US3] `_resolve_subagent_tools`, `_SubagentDomainPlugin`, `_build_subagent_registry` and `_SUBAGENT_REGISTRY` in `app/agent/subagent_tools.py` *(FR-012)*
- [x] T035 [US3] `RunSubagentArgs`, the closed `SubagentName` enum and the Ecorp `run_subagent` in `app/agent/subagent_tools.py`; the per-product closure in `app/agent/subagent_domain_tools.py`; the `read_only` declaration in `app/agent/tools.py` (`TOOL_CAPABILITIES`) *(FR-013, FR-014, SC-003)*
- [x] T036 [US3] The core of `_run_subagent_impl` — ctx check, fresh messages, the specialist's model, budgets and recursion limit, answer extraction and scrubbing, the budget-exceeded message — in `app/agent/subagent_tools.py` *(FR-015, FR-016, FR-017, FR-018)*
- [x] T037 [P] [US3] The routing of `run_subagent` straight to execution and the fold of `subagent_spend` into the budget check, in `app/agent/graph_routing.py` *(FR-014, FR-019)*

### Open follow-ups for User Story 3 (not built) — **A1 (task), A6**

- [ ] T038 [US3] **A1 — write the failing test first**: in `tests/agent/test_tools.py` (`TestRunSubagentTool`) assert both `RunSubagentArgs` and a per-product schema from `make_domain_subagent_tool` reject a `task` over the bound. Fails today (`task` only rejects blank) *(FR-027)*
- [ ] T039 [US3] **A1 — fix**: one shared bound for `task` (proposal: 4000 characters, well inside the nested 4000-token budget), applied in `app/agent/subagent_tools.py` and `app/agent/subagent_domain_tools.py` — not two copies that can drift *(FR-027)*
- [ ] T040 [US3] **A6 — decide and record**: decide whether a specialist's answer is framed as data when it re-enters the parent. If yes: failing test first in `tests/agent/test_tools.py` (the returned tool message is delimited and the system rule covers it), then the framing in `app/agent/subagent_tools.py` and `app/agent/subagent_domain_tools.py`. Either way, record the decision and its reason in `GRAPH_PATTERNS.md` pattern 46 *(A6)*

**Checkpoint**: US3 works as built; the read-only guarantee is intact.

---

## Phase 6: User Story 4 — Delegated work is bounded, accounted for and visible (Priority: P2)

**Goal**: Budgets, spend, outcome counts, tracing, concurrency — and a clean end to every run.

**Independent Test**: Run a completed, an over-budget and a timed-out delegation; compare counters, ledger, parent budget and retained memory.

### Tests for User Story 4

- [x] T041 [P] [US4] Spend folds into the parent's budget, resets next turn, accumulates for parallel calls; concurrent delegations never cross-wire — in `tests/agent/test_safety_budgets.py` (`TestSubagentSpendBudget`, `…subagent_spend_recorded_in_one_turn_does_not_survive_into_the_next`, `…parallel_subagent_entries_within_one_turn_still_accumulate`) and `tests/agent/test_concurrent_turns.py` (`TestSubagentCallUnderConcurrency`) *(FR-019, FR-022)*
- [x] T042 [P] [US4] A nested run's tokens never reach the client's stream; its tool activity does, tagged — in `tests/agent/test_streaming_terminal_events.py` (`TestSubagentEventsDontLeakIntoTheMainStream`) and `tests/agent/test_nodes.py` *(FR-021)*
- [x] T043 [P] [US4] The graph cache reuses per (product, specialist), the timeout outcome counter, the ledger row with the derived thread id — in `tests/agent/test_tools.py` (`TestSubagentGraphCache`, `TestRunSubagentImpl::test_timeout_raises_and_is_recorded`, `…test_records_usage_to_the_ledger_with_a_derived_thread_id`) *(FR-020, FR-022, SC-005)*

### Implementation for User Story 4

- [x] T044 [US4] The outcome counter and duration histogram, the ledger write, the derived nested thread id and the compiled-graph cache in `_run_subagent_impl` and `reset_subagent_graph_cache` in `app/agent/subagent_tools.py` *(FR-019, FR-020, FR-022, SC-005)*
- [x] T045 [P] [US4] Callback and metadata threading into the nested run, and the stream guard that keeps its tokens out of the answer, in `app/agent/subagent_tools.py` and `app/agent/runtime_stream.py` *(FR-021)*

### Open follow-ups for User Story 4 (not built) — **B13, B14**

- [ ] T046 [US4] **B14 — write the failing test first**: in `tests/agent/test_tools.py` (`TestRunSubagentImpl`) add a test that a run which returns a 500-token step and then times out records 500 tokens through `usage_ledger.record_usage` (and that an exception-raising run does too). Fails today (reproduced — quickstart *Scenario B14*) *(FR-024, SC-007)*
- [ ] T047 [US4] **B13 — write the failing test first**: in `tests/agent/test_tools.py` (`TestSubagentGraphCache`) add a test that after N cached runs — completed, timed out and errored — the cached graph's store holds no thread belonging to a finished run. Fails today (reproduced — *Scenario B13*: 100 runs → 100 threads) *(FR-023, SC-006)*
- [ ] T048 [US4] **B14 + B13 — fix** in `_run_subagent_impl` in `app/agent/subagent_tools.py`: on **every** exit path first read the run's last checkpoint (`aget_state`) for tokens and cost and record usage, then call `adelete_thread(<nested thread id>)` on the graph's store (present in the installed store — verified). Decide in the same change whether the timeout path should return a failure message **plus** a spend entry instead of raising, so the parent's live budget also sees it *(FR-023, FR-024)*
- [ ] T049 [US4] **B13 — wording**: replace "ephemeral"/"throwaway" for the nested store in `_run_subagent_impl`'s comments in `app/agent/subagent_tools.py` and in `GRAPH_PATTERNS.md` pattern 46 with the real lifecycle; add B13/B14 and their fixes to pattern 46 *(FR-023)*

**Checkpoint**: US4's bounds and counters hold; its *accounting* and *cleanup* are verified only after T046–T048.

---

## Phase 7: User Story 5 — Authors add procedures and specialists by editing files, and every deployment gets them (Priority: P3)

**Goal**: File-based authoring with safe skipping, an index in step with disk, and the same catalogs in every deployment.

**Independent Test**: Add a valid, a malformed and a duplicate file; then start the *containerized* stack and confirm the bundled entries are present.

### Tests for User Story 5

- [x] T050 [P] [US5] Loader accept and skip rules for both catalogs — frontmatter, name/description, body, YAML, `domains`/`tools`/`model` types, missing folder, malformed skipped, duplicate keeps first — in `tests/agent/test_skills.py` and `tests/agent/test_subagents.py` *(FR-001, FR-002, FR-010, SC-008)*

### Implementation for User Story 5

- [x] T051 [US5] The `restart-all` recipe in `Makefile` runs `make index-skills` (`scripts/index_skills.py`) after `make ingest` — the one automated bootstrap, host-native only *(FR-004)*

### Open follow-ups for User Story 5 (not built) — **B16, A2, A3**

- [ ] T052 [US5] **B16 — write the failing test first**: new `tests/test_dockerfile_catalogs.py` parsing the repo `Dockerfile` and asserting it copies `skills/` and `subagents/` into the image. Fails today (`COPY app/` only — reproduced, quickstart *Scenario B16*) *(FR-026, SC-009)*
- [ ] T053 [US5] **B16 — fix**: `COPY skills/ ./skills/` and `COPY subagents/ ./subagents/` in `Dockerfile` (before the ownership change); decide in the same change whether `scripts/` also ships — T059 needs it to index inside a container *(FR-026)*
- [ ] T054 [US5] **B16 — CI smoke (`docker`)**: in the `docker-build` job of `.github/workflows/ci.yml`, after building, run the image and assert both catalogs are non-empty and `run_subagent` is bound — the check a text test of the Dockerfile cannot make *(SC-009)*
- [ ] T055 [US5] **B16 — visibility**: at API and worker start, log a warning and increment a new `agent_catalog_empty_total{catalog}` (`app/core/metrics.py`) when either catalog is empty, so a repeat of B16 is loud; failing test first in `tests/agent/test_skills.py` and `tests/agent/test_subagents.py` *(FR-026)*
- [ ] T056 [US5] **A2 — write the failing test first**: in `tests/agent/test_tools.py` (`TestSkillSearch::test_missing_collection_degrades…`) assert the degrade also increments a counter. Fails today (log only) *(FR-026)*
- [ ] T057 [US5] **A2 — fix**: add `agent_skill_search_unavailable_total` to `app/core/metrics.py` and increment it in `_skill_search_impl` in `app/agent/tools.py` *(FR-026)*
- [ ] T058 [US5] **A2 — idempotent index, test first**: new `tests/scripts/test_index_skills.py` asserting two runs against a fake store produce the **same point ids** and never delete an existing collection; then change `scripts/index_skills.py` to `uuid5(name)` ids and create-if-missing (the script uses `uuid4` today, which is why it recreates and leaves a window with no catalog) *(FR-026)*
- [ ] T059 [US5] **A2 — bootstrap**: add a one-shot index step to the compose files (`docker-compose.yml`, `docker-compose.prod.yml`) and the release procedure so a fresh deployment builds the skills index; depends on T053's `scripts/` decision and T058's idempotence *(FR-026)*
- [ ] T060 [US5] **A3 — decide, then test first**: either (a) when `skill_search` returns a name absent from the cached catalog, reload once, rate-limited — failing test in `tests/agent/test_tools.py`: a fake index returning a skill that exists on disk but not in the cache is found after one reload — and implement in `app/agent/tools.py`; or (b) document "restart every process after `make index-skills`" in the README and `contracts/catalog-files.md` as the one step. Say which in the commit *(FR-028)*

**Checkpoint**: US5 is *verified* only after T052–T054 — until then a containerized deployment silently has no skills and no specialists.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T061 **A8 — configuration**: add `SKILLS_DIR`, `SKILLS_COLLECTION`, `SKILLS_SEARCH_TOP_K`, `SUBAGENTS_DIR`, `SUBAGENT_TIMEOUT_SECONDS` and `MAX_SUBAGENT_COST_USD_PER_RUN` with a one-line why each to `.env.example` (CLAUDE.md: every tunable has an entry); a test or lint that every `Settings` field is present would prevent a repeat (feature 004 A3 is the same class) *(A8)*
- [ ] T062 **A8 — comments**: correct the `max_subagent_cost_usd_per_run` comment in `app/core/config.py` (spend **is** folded into the parent turn) and the comments citing `tools.py::run_subagent` in `app/core/config.py` and `app/agent/graph.py` (it lives in `subagent_tools.py`) *(A8)*
- [ ] T063 **A8 — docs**: add B13–B16 and A1–A8 to "Extending Further" in `GRAPH_PATTERNS.md`, correct patterns 45 and 46, and make the README's onboarding-brief and `researcher` examples say they need the catalogs in the image (until T053) in `README.md` *(A8)*
- [ ] T064 After each fix, re-run `quickstart.md`'s scenario for it and delete its row from `plan.md` *Complexity Tracking*

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup** → **Foundational** → **User Stories**. Everything `[x]` already exists.
- **US1, US2, US3** (P1) need only Phase 2. **US4** (P2) extends US3's run. **US5** (P3) is independent of the others at the code level but its B16 fix is what makes *all* of them true in a container.
- **Polish** last — except **T061–T063's** B16 note, which lands with T053.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T052–T053 (B16 fix), T054 (smoke) | `Dockerfile`, `.github/workflows/ci.yml`, one new test | test-first; **first** — without it every other story is absent in containers |
| 2 | T026–T027 (B15, A7), T028–T029 (A4) | two `SKILL.md` tags, `subagent_tools.py`, `metrics.py`, one new test file | test-first; tiny; the pin test needs no other PR |
| 3 | T046–T049 (B14, B13) | `subagent_tools.py`, one test file, `GRAPH_PATTERNS.md` | test-first; **one** change in one function — keep together because the delete must follow the read |
| 4 | T055 (B16 visibility) | `app/api/main.py` and worker start, `metrics.py`, tests | small; after PR 1 |
| 5 | T056–T060 (A2, A3) | `index_skills.py`, `tools.py`, compose files, tests | needs the bootstrap and reload-vs-restart decisions; T059 needs PR 1's `scripts/` decision |
| 6 | T018–T019, T038–T039 (A1) | `tools.py`, `subagent_tools.py`, `subagent_domain_tools.py`, tests | small, independent |
| 7 | T020–T021 (A5), T040 (A6) | `tools.py`, `graph_skills.py`, pattern 46 | A6 is a decision, possibly docs-only |
| 8 | T061–T064 (A8) | `.env.example`, comments, README, `GRAPH_PATTERNS.md` | docs; can land any time |

PRs 2, 3, 6 and 8 are mutually independent; run them in parallel after PR 1.

### Parallel opportunities

- Setup T001–T004 and Foundational T005–T007, T009 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 4

```bash
# Tests together (different files):
Task: "T041 Spend and concurrency in tests/agent/test_safety_budgets.py and tests/agent/test_concurrent_turns.py"
Task: "T042 Stream isolation in tests/agent/test_streaming_terminal_events.py"
# Implementation together:
Task: "T044 Counters, ledger and cache in app/agent/subagent_tools.py"
Task: "T045 Callback threading in app/agent/runtime_stream.py"
```

## Implementation Strategy

### As-built order (what happened)

Skill packages (pattern 45) and subagent delegation (pattern 46) both landed on 2026-08-29 — two days after the Dockerfile (08-27), which has therefore never copied either folder. Domain scoping
followed on 09-03. The 09-09 fixes came out of live runs on the small model: the leading tool order, the `use_skill`-without-search guard, `run_python_in_sandbox` and the required-tool check. On 09-10
the compiled-graph cache and the fold of a run's spend into the parent's budget arrived in one change (the cache is where B13 begins); `run_subagent` joined the leading tier on 09-11; on 09-21 the
timeout became a setting after a CI failure and the default top-k was set to 1. The four defects sit at *seams the feature work never crossed*: an owner for a finished run's state, an exit path nobody
metered, a default written for a generic skill that two specific ones inherited, and a build file that predates the catalogs.

### Closing the open follow-ups (what to do next)

1. **PR 1 (B16)** now — it is one line per folder and until it lands this whole feature is absent in containers without a signal.
2. **PR 2 (B15, A7, A4)** — makes "catalogs ship correctly" a test, not a belief.
3. **PR 3 (B14, B13)** — the lifecycle fix; read, record, then delete.
4. **PRs 4–5** — make an empty or stale catalog loud, and make indexing automatic and idempotent.
5. **PRs 6–8** — bounds, small cleanups, docs.
6. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 + US3 (T001–T037) is the minimum that finds a procedure, scopes it and delegates safely. None of B13–B16 weakens tenant isolation or the read-only guarantee, so the feature is *safe* as it
stands — but not **fully correct** and, in a container, **not present**: B13 grows memory with use, B14 hides spend, B15 misleads three products and B16 removes the feature entirely.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (353 passed, 8 deselected) was re-run on 2026-10-02, plus the reproduction scenarios and the read-only inspection of the built image.
- Tier 2 (there is none for this feature) and the live tier were **not** run by this batch.
- Features 001 (the graph loop, retrieval and the output checks), 003 (the approval gate that makes "read-only" mean something), 005 (each product's tool set), 008 (the ledger and budgets) and 009 (the
  sandbox a skill may name) own behavior this feature relies on.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks; `make index-skills` recreates the skills collection (A2) and is safe only because that collection is a rebuildable cache.
