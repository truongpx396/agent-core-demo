# Specification Quality Checklist: Mandatory Approval and Exactly-Once Writes

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

- **This checklist grades requirements quality, not the system.** Two success criteria are marked
  *not met when this spec was written* (SC-010 and SC-011, bugs B3 and B4 — both fixed since, in #62 and #63). That is deliberate: a criterion the
  shipped system fails is more useful stated and flagged than omitted. A spec with failing criteria
  is still complete.
- **Two defects were reproduced, not inferred.** B3 (a stale "cancelled" marker makes a later
  approval end the run without running the action) and B4 (an unattended conversation stranded by a
  second pause, with a silent first reply) were each reproduced with a hermetic harness — fake
  model, in-memory store, no services. Evidence levels are stated in *Known gaps*; neither was
  reproduced against a real model, queue or chat-app connection.
- **A7 was found by audit, not by running anything:** each declared write tool was matched against the
  exactly-once wrapper by script, then tests were searched for any that would fail if the wrapper
  were dropped.
- **Technology neutrality.** `grep` for storage, framework, queue-protocol and channel names returned
  nothing; the chat-app channel is referred to generically. File-level evidence lives in `plan.md`
  and `research.md`.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
  None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 0 technology names, 29 FRs, 11 SCs,
   6 stories.
2. **Iteration 2 (claim verification before finalizing):** the audit statement "15 of 15" was
   incomplete — the four sandbox tools are declared dynamically; a second grep showed each is wrapped
   in all three domains that expose them. US6, SC-009 and A7 now say so. SC-008 originally said
   "within 15 minutes"; the alert rule is `increase(...[15m]) > 0` held `for: 15m`, so the wording
   became "once the degradation has persisted for 15 minutes".
3. **Iteration 3 (found while writing `plan.md` / `research.md`):** (a) the notification-sending tools
   (`escalate_to_human`, `handoff_to_human`, the ops post tool) are protected by layer one only — no
   target-level key for the message — recorded in the spec's *Residual duplicate window* gap and plan R1;
   (b) `GRAPH_PATTERNS.md` and two comments still describe a removed safety function — gap A11;
   (c) the cooperative cancel check is not wired into the resume path — gap A12, with FR-011 and SC-005
   amended to say so.
4. **Iteration 4 (`/speckit-analyze`, read-only; then my own corrections):** the analysis found that (a) FR-010
   has no test anywhere — research R6 cited a file that only tests the unattended helper, and task T053 claimed
   a paused-thread refusal test that does not exist; (b) SC-002 misclassified the escalation and handoff tools;
   (c) gap ids R1/A9/A10 were used in plan/tasks but unlabelled in the spec. Corrected: R6, T053, SC-002, labels
   added, and the missing test recorded as task T050 (tasks renumbered to stay sequential). Left for a decision,
   not changed: whether to clarify Principle IV for effects with no addressable target (C2), requirement-id
   traceability tags in tasks (U1), and `promtool check rules` in CI (U3).
5. **Iteration 5 (`/speckit-analyze`, then a correction after a wider test search):** (a) FR-010 has no test (T050);
   (b) a *claim* that no integration test touches the real constraints / consumer groups was too broad — consumer-group
   delivery and the thread lock **are** tested against a real Redis; what is untested is the target-level `UNIQUE`
   constraints and, newly found, the `XAUTOCLAIM` reclaim path, which only a hand-written fake covers (gap A13, task
   T058).

7. **Iteration 7 (reconciliation, 2026-10-03):** the fixes merged since this spec (#62 B3; #63 B4; #65 A12; #66 A11; #67 feature 002's B2; #68 A7 and the pending-approval endpoint test) were folded into the spec, plan, research, data-model, contracts, quickstart and tasks; SC-005, SC-009, SC-010 and SC-011 are now met; task T005's claim that its six queue tunables have example-environment entries was found to be **false** (none has one — only the setting #63 added) and corrected; Tier 1 was re-run (465 passed, was 400).
