# Specification Quality Checklist: Core RAG Agent Turn Pipeline

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

- **Retrospective spec.** Written after the system was built, so every requirement was checked
  against the code rather than the original intent. Several drafting claims were corrected during
  that check (see *Validation iterations*); that is the main reason this checklist is trusted.
- **"Non-technical stakeholders" is a judgment call.** The audience of this platform is end
  users, operators and domain developers. Plain-language terms are used throughout, but a few
  capability words (re-ranker, classifier, checkpoint codes in FR-026/FR-033) are unavoidable
  because they are part of the observable contract. No product, framework or protocol names
  appear (`grep` for the usual suspects returned nothing).
- **Numeric defaults are in the spec on purpose.** They are tunables, stated so a reviewer can
  test "the bound exists and trips"; the plan (`plan.md`) names the setting that owns each.
- **Known gaps are in the spec, not hidden.** *Known gaps* carries: screening runs after history
  housekeeping (D1), the sub-run-spend reset bug (B1, reproduced), the answer cache ignoring
  conversation context (G3, read from code), and the error envelope not being universal (A2:
  six unemitted codes, three bypassing `error` paths).
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
  None are incomplete.

## Validation iterations

1. **Iteration 1 (against code, not just the spec text):** corrected (a) auto-inserted citation
   markers go on each source's best-matching sentence, not "when exactly one source matches";
   (b) on retry exhaustion only a missing marker or a too-short non-blank answer is kept, a
   misattributed one is replaced; (c) a tripped safety ceiling delivers the last content only if
   it still passes the quality checks; (d) registered error codes are never emitted (first
   counted as four, corrected to six in iteration 5); (e) first-use seeding checks stored state
   but is not atomic; (f) the relevance floor applies to document results only (memories skip
   re-ranking).
2. **Iteration 2:** SC-008 referred to dependencies "named in User Story 6" while the list lives
   in that story's independent test; reworded to point at it. Mechanical re-check: 0
   clarification markers, 0 technology names, 35 FRs, 9 SCs, 7 stories.
3. **Iteration 3 (found while writing `plan.md`):** the draft said a screened-out request causes
   *no* model call. In the as-built graph history housekeeping runs before screening and can
   make one summarization call on an over-ceiling conversation. US2, FR-003, SC-003 and *Known
   gaps* now say so; the constitution deviation is recorded in `plan.md` Complexity Tracking.
4. **Iteration 4 (found while writing `data-model.md`, verified by running a hermetic repro):**
   FR-019 claimed every per-turn counter resets each turn. One does not (`subagent_spend`, bug
   B1). FR-019 now says so and points at *Known gaps*, which carries the repro. A second related
   bug (a stale `cancelled` flag) belongs to feature 003 and is recorded there, not here.
5. **Iteration 5 (found while writing `contracts/error-envelope.md`, by listing every
   `"type": "error"` emitter):** the envelope is not universal — six registry codes are never
   emitted (not four) and three `error` paths bypass the envelope, one forwarding raw `str(exc)`.
   FR-033 and *Known gaps* corrected. Also found the cache key ignores conversation context (G3).
6. **Iteration 6 (found while writing `tasks.md`; corrected in iteration 7):** no test *names* the cache's
   `_escape_tag` fix, so advisory A3 was added to `plan.md` and task T087 to `tasks.md`. **Iteration 7 corrected
   this**: the original wording said the fix had no regression test at all. Reading `tests/agent/test_concurrent_turns.py`
   showed a real-Redis test that uses hyphenated tenants and would fail if the escape regressed, so A3 is
   downgraded to "covered incidentally, not explicit, principal axis untested" and T087 narrowed accordingly.
7. **Iteration 7 (`/speckit-analyze`, read-only; then corrections):** (a) task T021 and quickstart/research claimed
   the hermetic Qdrant-store test proves the tenant filter sits in each prefetch — it does not; the *effect* is proven
   by a real-Qdrant integration test and the citations now say so; (b) task T058 overclaimed the retry-exhausted trust
   rule's node-level coverage and T075 claimed a retry-policy behavior test that does not exist (only a structural
   check) — both reworded; (c) gap ids D1/G2/G3/A2 were used in plan/research/tasks but unlabeled in the spec — labeled;
   (d) advisory A3 downgraded (see iteration 6's note). Left for a decision, not changed: requirement-id tags in tasks
   (traceability), and `promtool check rules` in CI.

8. **Iteration 8 (reconciliation, 2026-10-03):** the fixes merged since this spec (#62 B1; #64 A2's worker catch-all; #68 and #69 A3 and the cache's tag-escaping defect) were folded into the spec, plan, research, data-model, contract, quickstart and tasks; the *Known gaps* paragraph for B1 became *Resolved since this spec was written*; A2 was narrowed to what remains (the six unemitted codes, a refused resume without a `code`, the first-event deadline); Tier 1 was re-run (335 passed, was 324).
