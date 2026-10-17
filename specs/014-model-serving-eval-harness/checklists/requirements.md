# Specification Quality Checklist: Model Serving Seams and an Evaluation Harness

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-10-17
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
  - The models (bge-small-en-v1.5, all-MiniLM-L6-v2, gte-modernbert-base) and the product components (ML service, gateway
    alias) are named because the request fixes them; they are constraints, not design. No library, file layout, function or
    data structure is named. The "request shape of the hosted embedding APIs" is a compatibility requirement, not an API design.
- [x] Focused on user value and business needs
  - Every story is a maintainer or operator outcome: decide from evidence, never mix embedding spaces, attach a stronger model safely.
- [x] Written for non-technical stakeholders
  - Pass **with a caveat**: the audience of every spec in this repository is its maintainers, and the spec uses their
    vocabulary (tenant, approval, ledger). The stories and success criteria read without code knowledge.
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
  - Three were resolved with the requester before writing: constitution amendment in scope; ML service serves bge-small and
    gte-modernbert while the app default stays; one operator-wide escalation switch.
- [x] Requirements are testable and unambiguous
  - Several numeric parameters are deliberately left to planning and named as such: the embedding parity tolerance (FR-025),
    the ceilings on batch size and input length (FR-021), the classifier deadline and recovery interval (FR-029, FR-031).
    Each has a testable shape (a stated, enforced bound); the value is a plan decision.
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
  - SC-007 and SC-012 name the gateway's `embed` alias, which is the user-visible configuration point, not an implementation.
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
  - Out of scope is stated in *Assumptions*: vendor choice, chat model, replacing the reranker or the input screen, per-tool-round
    or mid-turn routing, per-tenant escalation control.
- [x] Dependencies and assumptions identified
  - Includes what planning must verify (stored text sufficient to re-embed; existing retrieval-outage degrade path; unpriced-alias rule).

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
  - FR-001..014 ↔ Story 1 scenarios and SC-001..004; FR-015..026 ↔ Story 2 and SC-005..007; FR-027..039 ↔ Story 3 and
    SC-008..011; FR-040..042 ↔ Story 3 scenario 1, SC-012 and the hermetic-tier scenario.
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Constitution touchpoints (for the plan's Constitution Check)

Not part of the generic checklist; recorded so planning does not have to rediscover them.

| Principle | Where this feature touches it | Spec location |
|---|---|---|
| I Tenant isolation | re-embed keeps tenant/owner scoping; the classifier sees only the current message and no tenant ids; no tenant traffic used for training | FR-020, FR-031, FR-039 |
| II Approval | routing is advisory and cannot change any approval requirement | FR-035, Story 3 scenario 8 |
| III Fixed tools | no tool is added; no change | — |
| IV Exactly-once | routing does not change duplicate protection; a resumed turn keeps its model | FR-034, FR-035 |
| V Bounded failure | deadline on the classifier, bounded fallback recovery, bounded model-load retry, a metric on every degrade path, alerts on sustained ones | FR-019, FR-024, FR-026, FR-029, FR-037 |
| VI Untrusted content | classifier runs after input moderation and treats the message as data | FR-031 |
| VII Test discipline | default suite stays hermetic; live pieces marker-gated; autouse mock for the new turn-path call | FR-012, FR-042 |
| VIII Honest gaps | deliberate gaps listed and to be documented | FR-041, *Assumptions* |
| Architecture: LLM access | **conflicts as written**; amended first | FR-040, Story 3 scenario 1 |
| Spec Kit gate: adds no `mutating`/`outward` tool | stated here: none added | — |

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
- All items passed on the first validation pass; the one wording defect found while validating (an overclaim about
  non-English behavior in *Edge Cases*) was corrected in the spec.
