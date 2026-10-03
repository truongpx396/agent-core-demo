# Specification Quality Checklist: Observability and Cost Governance

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-10-03
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

- **This checklist grades requirements quality, not the system.** Six success criteria are marked *not met* (SC-004, SC-007, SC-008, SC-009, SC-010, SC-011 — gaps B17, B20, B18/B19, B21, A1, B22) and thirteen
  requirements are stated as gaps (FR-009, FR-011, FR-012, FR-015, FR-016, FR-021 to FR-028). That is deliberate: a criterion the shipped system fails is more useful stated and flagged than omitted. A spec with failing
  criteria is still complete. FR-004 is "partly met" and says so.
- **How each defect was established** (the evidence level matters more than the label):
  - **B17 was reproduced against the real graph**: a real compiled graph with an in-memory checkpoint store, a model that returned a 500-token step and then stalled past a shortened request timeout, and a recording
    stand-in for the ledger — the checkpoint held 500 tokens, nothing was recorded.
  - **B18 was reproduced against the installed tracing SDK**, with dummy keys and the discard port so nothing left the machine, and again with no keys at all.
  - **B19 was reproduced at function level** with a 0.5 s blocking stand-in for the 5 s HTTP call and a 10 ms heartbeat. The *rejected-key* trigger (a non-admin key refused by the admin endpoint) is the likely real
    cause and was **not** run.
  - **B20 was reproduced against a real Postgres** (an ephemeral container, removed afterwards) with the three statements copied from the ledger module. The "40 crashes = the $20 ceiling" figure is **arithmetic**,
    shown by a loop that adds 0.50 thirty-nine more times — it is not a measurement of 40 real crashes.
  - **B22 was reproduced against a script's real startup path**: only logging is configured, and the global meter provider is the no-op proxy. Whether a *short-lived* process would flush a periodic exporter at exit was not
    tested (there is no exporter to flush).
  - **B21 was established by reading** (the reader, the alert file, the readiness probes) and **not exercised against a running stack**.
  - **A1–A10 were found by reading**; A3 was additionally **measured** on a throwaway Postgres (warm cache, one machine — the absolute figures are small; the finding is that the work scales with a tenant's history), and
    A4 and A5 were checked with scripts (every metric against every alert and dashboard query; every `.labels(...)` call site against its declaration), which found no drift today.
- **A boundary the spec corrects in itself.** The first draft said logs, metrics *and traces* carry metadata only. Traces carry the user's text, the model's prompts and answers and tool inputs and outputs — by design, and the
  design doc says so ("outside Langfuse"). The spec now says logs and metrics, and records the conflict with the constitution as **A10**, a decision for the constitution process, not for this document.
- **Boundary with features 001–007.** The per-turn ceilings (001), the queue, workers and the first-event deadline (003, 004) and the delegated-run metrics and ledger rows (007) are *used* here and cross-referenced; the
  notifier's sandbox and crawler callers (009) are referenced, not re-specified. This batch links to no unmerged feature's files, so it merges independently.
- **Technology neutrality.** The spec body names no metrics, tracing, logging, dashboard or database product; it keeps "metrics", "logs", "trace", "dashboard" and "alert". Product names appear only in the retrospective
  note's paths and in *Known gaps*, where they are evidence.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`. None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 28 FRs, 11 SCs, 5 stories, 16 gap ids (B17–B22, A1–A10) all defined in plan *Complexity Tracking*, in `research.md` Part C and carried by a task. (The cross-check
   script only recognizes single-digit `B` ids, so B17–B22 were confirmed by grep: each appears in the spec, plan, research, data-model, quickstart and tasks, and in the contract that states it.)
2. **Iteration 2 (claim verification before finalizing):** (a) the draft said traces carry metadata only (FR-007, US5, SC-002) — **wrong**; corrected, and A10 added; (b) B18 was first scoped to "where tracing is configured" —
   re-tested with no keys and corrected: a disabled client still starts three threads, so the leak is in the default configuration; (c) readiness was described as checking "the embedding service" — it is the ML (rerank and
   moderation) service; (d) "the request counter is incremented only by a worker" was softened to "where a turn runs"; (e) SC-008 first read "+600 threads" — an extrapolation — and now states the measured 60 after 10 turns;
   (f) the scheduled scripts use two principals (`ops-cron`, `sales-followup-cron`), not one; (g) a cited test class was wrong (`TestToolMetricsViaCallback` → `TestToolCallAuditLog`); (h) the as-built narrative was first
   written from the docstrings and now uses dates from `git log`; (i) A9 was extended to alert and dashboard validation after finding no CI step and no test for either.
3. **Iteration 3 (found while writing the contracts, quickstart and tasks):** listing which processes call the telemetry configuration exposed B22 (four scripts never do), which was then reproduced; reading the reservation SQL
   statement by statement exposed B20, which was then reproduced against a real database; the per-tool audit lines are correct but the spec first over-claimed what the *trace* callback records.
4. **Iteration 4 (`/speckit-analyze`-style cross-check, read-only):** 0 uncited requirements; SC-003 and SC-006 had no task and are now cited; T044 had no path and now has four; 0 plan gaps without a task; 0 tasks citing an
   undefined id; every relative link resolves; 61 tasks (34 built, 27 open), sequential, every story phase labelled; the six paths cited as not-yet-existing are the six new files the open tasks create.
