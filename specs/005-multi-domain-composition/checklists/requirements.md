# Specification Quality Checklist: Multi-Domain Composition (Support, Ops, Sales on One Graph)

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

- **This checklist grades requirements quality, not the system.** Four success criteria are marked *not met* (SC-005, SC-007, SC-009,
  SC-010 — gaps B7, B8, A1, A7). That is deliberate: a criterion the shipped system fails is more useful stated and flagged than omitted.
  A spec with failing criteria is still complete.
- **Two defects were reproduced, not inferred.** B7 (any principal of a tenant can read, escalate and comment on any ticket in it) was
  reproduced with a stand-in database that matches rows purely on the predicates a statement carries — **statement level; not run against a
  real database or a real chat app**. B8 (one failing follow-up aborts the sweep) was reproduced against the real `run_followup_sweep` with a
  stub store and model. Evidence levels are stated in *Known gaps*.
- **A7 was established by running code, not only reading it**: every tool's argument schema was dumped and the free-form string fields counted
  (41 fields across 31 tool definitions in the domain modules, none with a maximum length; the default assistant's `add_note` and `remember` have
  bounds) — then confirmed by `grep` for `max_length` under `app/domains`.
- **Boundary with features 001–004.** The pipeline (001), identity/ownership and the global ops data (002), the approval and exactly-once rules
  every domain tool follows (003) and routing to a pool (004) are *used* here and cross-referenced; the skill/subagent catalogs (007) and the
  sandbox/crawl tools (009) are referenced, not re-specified.
- **Technology neutrality.** The body of the spec names no storage, framework, metrics or chat-app product. It keeps the generic phrases "parameterized
  query" and "scheduled job". Product names appear only in *Known gaps*, where they are evidence.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`. None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 29 FRs, 10 SCs, 6 stories, 9 gap ids all defined in plan *Complexity Tracking* and
   carried by a task.
2. **Iteration 2 (claim verification before finalizing):** (a) SC-002 first said "56 cases"; the contract test has 27 tools × 2 checks + 2 guards, so
   it now says 27 tools; (b) B7's note first said the docstring chose tenant-only scoping "on purpose"; it says only that the docstring *describes*
   the asymmetry; (c) the data model said the compiled graph's manifest is read by the "checkpoint-resume path" — the reader is
   `runtime.py::_ensure_seeded_async` (seeding a thread with the domain's prompt); (d) A7 first said "18 tools" and "closed enums for the incident status"
   — the count is 31 tool definitions (19 domain tools + 12 sandbox wrappers; 28 affected, 41 fields) and `status` is a free-form `str | None`;
   (e) the quickstart's "no integration test touches these stores" was true but incomplete — the one live test that drives domain tools covers only the
   two crawl tools and says it avoids the stores.
3. **Iteration 3 (found while writing the contracts and data model):** (a) the support and ops domain docstrings contradict the registry (A1), and
   `handoff_to_human`'s description says "hot" while the code sets `handed_off`; (b) the sweep posts *then* marks done (A2) and sweeps only the default
   tenant (A3); (c) the ops thresholds are a copy of the alert rules with no test (A4); (d) the ops domain lacks the negative test support and sales
   have (A5).
4. **Iteration 4 (`/speckit-analyze`-style cross-check, read-only):** 0 uncited requirements, 0 criteria without a task, 0 plan gaps without a task;
   one task line carried no path and was given one; every relative link resolves.
