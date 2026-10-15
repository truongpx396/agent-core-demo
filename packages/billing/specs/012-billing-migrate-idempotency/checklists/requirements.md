# Specification Quality Checklist: Billing Migrations and Idempotency Conflicts

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-10-14
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) — the stories, requirements and success criteria are behavioural. Mechanism-level choices (a transaction per script, an advisory lock, a JSON column) are confined to "Assumptions and Decisions" (D1-D16), which is this repository's convention (spec 011 does the same) so the plan does not re-open them.
- [x] Focused on user value and business needs — an operator can bring a database with money in it up to date safely; a caller that retries cannot silently lose a request.
- [x] Written for non-technical stakeholders — the "Why this is shaped the way it is" section explains both gaps and why the second depends on the first without assuming the code.
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain — none were needed; each open choice is a numbered decision with its reason.
- [x] Requirements are testable and unambiguous — FR-001 to FR-024 each name an observable outcome; FR-010 fixes the exit statuses.
- [x] Success criteria are measurable — SC-001 to SC-007 carry counts, exit statuses and time bounds.
- [x] Success criteria are technology-agnostic (no implementation details) — they describe outcomes (applied once, refused naming fields, exit within twelve seconds); the one concession is naming the exit status, which is the operator-facing contract.
- [x] All acceptance scenarios are defined — three stories, 7 + 8 + 3 scenarios.
- [x] Edge cases are identified — nine, including the unknown recorded script, the role without DDL rights, legacy rows, and the cross-tenant case.
- [x] Scope is clearly bounded — "Not in this feature" names holds, the event store, notifications, the service shell and turnkey configuration; D15 fixes two PRs.
- [x] Dependencies and assumptions identified — the dependence of the conflict on the tool (for the column), the PostgreSQL version, and the role's rights.

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria — each FR maps to a scenario or success criterion.
- [x] User scenarios cover primary flows — empty, adopted and drifted databases; concurrent runs; a busy table; a failing script; identical and differing retries; the two legitimate-difference webhook callers; legacy rows; the operator command.
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification — see the first item.

## Notes

- Iteration 1 passed every item. One factual correction was made while validating (the `CREATE OR REPLACE TRIGGER` requirement is PostgreSQL 14, not 15).
- The two decisions most worth a reviewer's attention: **D1** (the scripts stay repeatable rather than becoming immutable numbered migrations) and **D11** (reason and actor are compared in the strict default).
