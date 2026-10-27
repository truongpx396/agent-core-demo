# Specification Quality Checklist: Edge authentication and rate limiting (authlimit)

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-10-27
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) — the requirements and success criteria speak of "the front door", "the service" and "the authorization server". Technology names (Go, Caddy, Casdoor, Redis, PKCE) appear only in Decisions, Assumptions, Constitution check and Delivery, because the maintainer fixed them.
- [x] Focused on user value and business needs
- [ ] Written for non-technical stakeholders — **not met, deliberately**: like specs 001-017 this one is written for the maintainers and reviewers of a security-critical service, and uses their vocabulary (token, issuer, audience, replica).
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain — the one real fork (what happens to production, whose deploy workflow runs on every push to `main`) was settled by the maintainer's note that production is not running (A1).
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable (SC-008's latency figures are labelled proposed targets, to be measured)
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded ("Not built", Decisions 4 and 6, A1 and A10)
- [x] Dependencies and assumptions identified (spec 017 T021 and T023, the deploy workflow, Casdoor's measured behaviour)

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria (each FR maps to a story scenario or a success criterion)
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification (outside the sections named above)

## Notes

- Unverified claims the spec depends on, to be tested first in planning: whether `forward_auth` forwards the `Authorization` header to the service, whether a client-sent identity header can survive, whether Casdoor's `JWT-Standard` token carries an issue time (FR-002 requires one), and the added latency (SC-008).
- A1 was revised after the maintainer confirmed that the app is not running in production: identity mode is the default and the production files are wired for it (the earlier draft kept header mode as the default to protect a live deployment).
- The forward_auth assumptions were tested on Caddy 2.11.7 and are recorded in A8 and `research.md`; the open item left is whether Casdoor's `JWT-Standard` token carries an issue time.
- The two-PR size estimate in "Delivery" is a first guess and will exceed the repository's guidance; it is disclosed, not hidden.
- Open for the maintainer: the constitution amendment for non-Python services (Constitution check) is a HUMAN sign-off.
