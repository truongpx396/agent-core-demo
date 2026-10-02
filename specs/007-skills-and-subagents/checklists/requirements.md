# Specification Quality Checklist: Skills and Subagents

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-10-02
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- **This checklist grades requirements quality, not the system.** Five success criteria are marked *not met* (SC-002, SC-006, SC-007, SC-009, SC-010 — gaps B15, B13, B14, B16, A4/A7) and six
  requirements are stated as gaps (FR-023 to FR-028 — B13, B14, B15, B16/A2, A1, A3/A4/A7). That is deliberate: a criterion the shipped system fails is more useful stated and flagged than omitted.
  A spec with failing criteria is still complete.
- **How each defect was established** (the evidence level matters more than the label):
  - **B13 and B14 were reproduced** with hermetic harnesses (a reusable fake model, a recording ledger, a shortened timeout; no services): 100 delegations left 100 retained threads, and a 500-token run that timed
    out recorded nothing. The harnesses were temporary files under `tests/agent/` (so the autouse mocks apply) and were deleted; `quickstart.md` gives the steps.
  - **B15 was reproduced against the real catalog** with a read-only script: the visible skills per product, a real `use_skill` call in the support product, and each product's resolved tool list.
  - **B16 was established by inspecting the built API image** (read-only `docker run`: no `/app/skills`, `/app/subagents` or `/app/scripts`; both catalogs empty; no `run_subagent`) and by reading the Dockerfile,
    both compose files and the release workflow. The image inspected was created after the Dockerfile's last change. The **production** image was *not* inspected; that it matches rests on the release workflow
    building the same Dockerfile.
  - **A1–A8 were found by reading** and each says so: callers of the reload hooks (A3), grep of `.env.example` (A8), a search for any test calling the real loaders (A7), the shipped skill text (A5).
- **The read-only guarantee was checked, not assumed.** Resolving every shipped specialist against each product's real capability table dropped **nothing**, and the nested graph's retained approval node is unreachable
  because every resolved tool is read-only. The Principle II verdict is therefore PASS, with the residual reliance stated (the nested run has approval off, so the resolver is the whole guarantee).
- **Boundary with features 001–006.** The graph loop and output checks (001), the approval gate (003), each product's tool set (005), the ledger (008) and the sandbox (009) are *used* here and cross-referenced; the
  shared vector-search code is feature 001's. This batch links to no unmerged feature's files, so it merges independently.
- **Technology neutrality.** The spec body names no vector store, graph framework, queue or database. It does keep the two tools' model-facing names, two metric names and the word "enum", because they *are* the
  interface the assistant and operators see; product and library names appear only in *Known gaps*, where they are evidence.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`. None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 28 FRs, 10 SCs, 5 stories, 12 gap ids (B13–B16, A1–A8) all defined in plan *Complexity Tracking*, in `research.md` Part C and carried by a task. (The
   cross-check script only recognizes single-digit `B` ids, so B13–B16 were confirmed by grep: each appears in the spec, plan, research, data-model, quickstart and tasks, and in the contract that states it.)
2. **Iteration 2 (claim verification before finalizing):** (a) a generalized "every named tool must exist in the product" scan also flagged `deal-economics` (sales) for `calculator`, which that skill names only to
   say it cannot do the job — so it is **not** a B15 victim, and the scan's false positive became the explicit exception list in the pinning test (T026); (b) the plan first described the specialists as "one per
   product tag plus ops' two" — corrected to support 1, sales 1, ops 2 and the Ecorp-only `researcher`; (c) I had attributed the use-skill-without-search and required-tool helpers to `graph_routing.py` and
   `graph_skills.py` — they live in `graph_loop_guards.py` and `graph_output_guardrails.py` (fixed in the plan, the skill contract and the tasks); (d) the first Tier 1 command omitted the support product's domain
   tests (336 passed) — the documented command includes them (353 passed, 8 deselected), and the stale count was removed; (e) the as-built narrative was first written from the docstrings — it now uses dates from
   `git log`; (f) the live-test command used a marker expression I had not run — replaced by the one the Makefile uses.
3. **Iteration 3 (found while writing the quickstart and tasks):** B16 started as a suspicion from reading one `COPY` line and was **confirmed against the built image before being written down**; the config comment
   that says a specialist's spend is not folded into the parent turn contradicts the reducer and its tests (A8); the `use_skill` reminder for `run_command_in_sandbox` is a branch no shipped skill can reach (A5);
   the indexing script's random point ids are *why* it must recreate the collection (A2's idempotence task).
4. **Iteration 4 (`/speckit-analyze`-style cross-check, read-only):** 0 uncited requirements; SC-003 and SC-005 had no task and are now cited; 0 plan gaps without a task; 0 tasks citing an undefined id; every
   relative link resolves; 64 tasks (36 built, 28 open), sequential, every story phase labelled; the three paths cited as not-yet-existing are the three new test files the open tasks create.
