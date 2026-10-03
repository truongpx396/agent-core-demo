# Specification Quality Checklist: MCP, Web Crawl and Sandbox Integrations

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

- **This checklist grades requirements quality, not the system.** Six success criteria are marked *not met* — SC-005, SC-006, SC-009 and SC-010 outright, and SC-007 and SC-008 in their second halves (gaps B26, B24, B27, A7, B23,
  B25) — and eleven requirements are stated as gaps (FR-009, FR-010, FR-011, FR-014, FR-015, FR-016, FR-022, FR-025, FR-026, FR-028, FR-029). That is deliberate: a criterion the shipped system fails is more useful stated and flagged than
  omitted. A spec with failing criteria is still complete.
- **How each defect was established** (the evidence level matters more than the label):
  - **B23 was reproduced inside the real container image** (read-only `docker run --rm`): the bridge script is absent, the sandbox and browser libraries are present, and the catalogue loader returned 0 tools after the child printed
    "can't open file". The *production* image was not inspected; the release workflow builds the same Dockerfile.
  - **B24 was reproduced with the real session code against a stand-in sandbox service** that implements the documented tool shapes and filters by tag equality. The **real service's** filter was not exercised, and **no real
    sandbox was driven at all** — its API key lives in a blocked file. The collision itself is a property of the string rewrite, which does not depend on the service.
  - **B25 was reproduced at function level** with the crawler and the store patched; nothing was fetched. That the stored note is replayed is shown by the real tool implementations; that a model would obey the replayed line is
    **not** shown and not claimed.
  - **B26 and B27 were reproduced** with IP-literal hosts (resolved locally, no packet sent) and a 0.5 s stand-in for name resolution.
  - **A1–A9 were found by reading**; A3's one-megabyte figure was checked with a one-line call; A2's "no network egress" claim was left **unverified** on purpose, and the spec says so rather than confirming or denying it.
- **The read-only inspection of a real image and the use of throwaway data are the only external effects of this batch.** No sandbox command was run, no page was fetched, no service was started or stopped.
- **Boundary with features 001–008.** The approval gate and the call-id protection (003), the product tool sets and the whole class of unbounded free-form arguments (005), the ingestion fetch (006), the image gap that also hides the
  skill catalogs (007) and the breaker's metrics (008) are *used* here and cross-referenced; this batch links to no unmerged feature's files, so it merges independently. B23 and feature 007's B16 share one fix.
- **Technology neutrality.** The spec body names no sandbox product, browser product, protocol library or compose file; it keeps "sandbox", "headless browser", "page", "the Model Context Protocol" (the feature's subject) and
  the user-visible "Python script". Product and library names appear only in *Known gaps*, where they are evidence.
- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`. None are incomplete.

## Validation iterations

1. **Iteration 1:** mechanical checks — 0 clarification markers, 29 FRs, 10 SCs, 5 stories, 14 gap ids (B23–B27, A1–A9) all defined in plan *Complexity Tracking*, in `research.md` Part C and carried by a task. (The cross-check script only
   recognizes single-digit `B` ids, so B23–B27 were confirmed by grep: each appears in the spec, plan, research, quickstart and tasks, and in the contract that states it.)
2. **Iteration 2 (claim verification before finalizing):** (a) I had written that the ingestion fetch offloads the address check to a thread — it calls it directly, so B27 is in the shared guard's *use*, not one caller (fixed); (b) A1 first said
   the databases "publish plain-HTTP interfaces" — the databases and queue do not; the vector store, object storage and model proxy do (fixed); (c) the probe tally "13 of 16 as intended" was 14 of 16; (d) "the page reader works in a container" became
   "can, given the service's token"; (e) the live crawl test's marker is `integration`, not `crawl` (fixed in the plan); (f) the MCP child-process environment claim was checked against the installed SDK, whose default is a six-variable allowlist;
   (g) the as-built narrative now uses dates from `git log`; (h) the "no egress" claim is recorded as unverified rather than repeated.
3. **Iteration 3 (found while writing the contracts, quickstart and tasks):** the bridge's own docstring ("this process runs as a bare host process") led to inspecting the image (B23); the metadata-rules docstring ("a colon alone failed every
   call") led to asking whether the rewrite was injective (B24); comparing the system prompt's framing rule with the three tools' return values exposed B25; writing the tasks exposed that a missing bridge is neither retried nor counted by the breaker nor
   metered (A9).
4. **Iteration 4 (`/speckit-analyze`-style cross-check, read-only):** 0 uncited requirements; SC-002 had no task and is now cited; 0 plan gaps without a task; 0 tasks citing an undefined id; every relative link resolves; 55 tasks (29 built, 26 open),
   sequential, every story phase labelled; the paths cited as not-yet-existing are the five new test files and one SQL file the open tasks create, plus two *stale* paths (`app/mcp_server.py`, `app/mcp_client.py`) that task T054 cites in order to correct.
