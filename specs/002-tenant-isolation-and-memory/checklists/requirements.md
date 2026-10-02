# Specification Quality Checklist: Tenant Isolation and Cross-Session Memory

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

- **This checklist grades requirements quality, not the system.** A spec can be well-formed while
  the system it describes has gaps. This feature's *Known gaps* section records one verified
  isolation gap (B2), a deployment-level authentication gap, and four smaller ones; none of them
  make the spec incomplete — they are the point of writing it retrospectively.
- **Evidence levels are stated per gap**: B2 was reproduced at the graph level (fake model,
  in-memory store) and its endpoint behavior established by reading the code; it was *not*
  reproduced against a running stack. The proxy finding is from reading the shipped
  configuration. The rest are from reading code and grepping for callers/tests.
- **Technology neutrality.** `grep` for storage, framework and proxy names returned nothing outside
  a deliberately generic reference; file-level evidence lives in `plan.md` and `research.md`.
- **Success criteria SC-001…SC-008 are outcomes, not implementation.** Note SC-008 is satisfied for
  *reads* of another person's conversation; B2 shows the equivalent guarantee does not hold for
  *continuing* one.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
  None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 25 FRs, 8 SCs, 6 stories. Three
   evidence references to a deployment file name and "SQL" inside *Known gaps* were reworded to
   generic terms so the spec stays technology-neutral; the file-level references move to
   `plan.md` / `research.md`.
2. **Iteration 2 (claim verification before writing):** every claim in *Known gaps* was either
   reproduced or tied to a specific read/grep: B2 (reproduced + read), no authentication in the
   shipped proxy (read), no caller of the deletion function (grepped `app/` and `scripts/`), no
   cross-tenant real-backend test (grepped `tests/live`, `tests/integration` — **this claim was wrong; see
   iteration 4**).
3. **Iteration 3 (`/speckit-analyze`, read-only; then my own corrections):** found that (a) no test asserts that a
   request lacking an identity header is rejected — task T028 claimed API-level coverage but the cited test only
   checks that the UI page mentions the header names (corrected; new task T034); (b) FR-013 (`/usage` tenant
   scoping) had a test but no task (new built task T021); (c) the pending-approval endpoint has no API-level test
   (new task T046); (d) a `T0xx` placeholder in `plan.md` (now T022). Left for a decision: requirement-id
   traceability tags in tasks, and the E1 policy decision.
4. **Iteration 4 (correction after `/speckit-analyze` of feature 001's tests):** the claim "no cross-tenant
   real-backend test exists" was too broad. `tests/agent/test_concurrent_turns.py` (`integration` tier) tests
   document search across six concurrent tenants on a real Qdrant and the answer cache's tenant axis on a real
   Redis Stack. The spec's *Known gaps*, plan Principle VII/A4, research, data-model §2, the quickstart, the
   scoping contract and task T022 now say what is and is not covered. (Lesson recorded: grepping two directories
   is not grepping the suite.)
