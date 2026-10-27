---

description: "Task list for spec 018: edge authentication and rate limiting (authlimit)"
---

# Tasks: Edge authentication and rate limiting (authlimit)

**Input**: `specs/018-authlimit-edge-auth/` (spec.md, plan.md, research.md, data-model.md, contracts/authlimit.md, threat-model.md, quickstart.md)

**Prerequisites**: branch `feat/018-authlimit-edge-auth` off an up-to-date `main` (spec 017 merged); Docker for the manual tier; Go 1.26+ and the repo's `.venv`.

**Tests**: included. The spec (FR-034, SC-001, SC-010) and the constitution (Principle VII) require them.

**Organization**: tasks are grouped by user story. Delivery is in **two stages**, so the file is in two parts that follow delivery order: **Part A = PR 1** (the Go service on its own, Phases 1 to 10), **Part B = PR 2** (the Python side, Phases 11 to 18). Story labels are kept in both parts. Nothing in Part B is needed for Part A to be merged and run.

**Part A ships as three pull requests** (the maintainer's decision, after the first single pull request came to about 9,000 hand-written lines: see research.md "Size"). Each is stacked on the one before and builds and passes its own tests alone, because the Go packages form a one-way chain (the leaf packages, then the server on top of them). Part B stays "PR 2".

| Pull request | Tasks | What it holds |
|---|---|---|
| **1a**, verify and mint | T001, T007-T012, T016-T018, T034, T035, T048 | the module, the configuration, the error envelope, logging and metrics, the token verifier and its key cache, the internal signing keys and the identity they sign, the shared token vectors, the constitution amendment, the boundary test |
| **1b**, limit, revoke, serve | T002-T005, T013-T015, T019, T020, T024, T026, T027, T029-T031, T033, T038, T041-T043, T045, T046 | the route table, the token buckets, revocation, the HTTP service and its binary, the image, the alert rules, the CI image checks |
| **1c**, stack and proof | T006, T021-T023, T025, T028, T032, T036, T037, T039, T040, T044, T047, T049-T054 | the Casdoor applications, compose, Caddy, the Makefile targets, the smoke, latency and mutation scripts, the documentation and the measured results |

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1 chat request with a verified identity · US2 bad tokens stopped · US3 rate limits · US4 failure survival · US5 MCP routes · US6 revocation and lifetime · US7 observability
- **HUMAN**: needs the maintainer, not an agent

## Rules that bind every task

- Constitution Principle V: every wait has a ceiling; every degrade path has a metric and, where a person could be unaware, an alert. Principle VI: a token, key or claim value other than the tenant and principal ids never reaches a log, a metric label or an error body.
- A broad `except Exception` in Python needs `# noqa: BLE001 - <reason>`; every `# type: ignore` / `# noqa` carries a reason. Comments are long-form: say why.
- Defaults come from [contracts/authlimit.md](contracts/authlimit.md) section E; do not invent others.
- Do not run `make clean`, `clear-*`, `restart-all`. Do not run heavy test tiers locally (full `make test`, `make test-integration`, `make test-live`): run the targeted tests named in each task and leave the rest to CI.

---

# PART A: PR 1, the service on its own

## Phase 1: Setup

- [X] T001 Create the Go module skeleton from plan.md "Source code": `services/authlimit/go.mod` (module `authlimit`, the language version of the pinned `golang:1.26`), empty `services/authlimit/cmd/authlimit/main.go`, `services/authlimit/cmd/echo/main.go`, and `services/authlimit/internal/{config,extjwt,identity,limiter,revoke,server,errenv,telemetry}/` each with a package doc comment stating the invariant it owns.
- [X] T002 Add dependencies to `services/authlimit/go.mod` and `go.sum`: `github.com/golang-jwt/jwt/v5` v5.3.1, `github.com/redis/go-redis/v9` v9.23.0, the OpenTelemetry Go SDK metrics packages with the OTLP/HTTP exporter, and `github.com/alicebob/miniredis/v2` v2.40.0 (tests). Record the exact versions resolved in research.md R4.
- [X] T003 [P] Write `services/authlimit/Dockerfile`: build stage `golang:1.26@sha256:37cfcca63222ec499bec5e8ad7aad52dbf7529db36d5812b8eae2f0621aa92d6` (re-read the digest with `docker images --digests`), static build of `/authlimit` and `/echo`; runtime `gcr.io/distroless/static` non-root pinned by digest (read it and pin it); no shell, no package manager; `ENTRYPOINT ["/authlimit"]`, `CMD ["serve"]`. Add `services/authlimit/.dockerignore`.
- [X] T004 [P] Add Makefile targets `authlimit-test` (`cd services/authlimit && go vet ./... && go test -race ./...`), `authlimit-build` (`docker build -t authlimit:dev services/authlimit`), and list them in `.PHONY` and the help text of `Makefile`.
- [X] T005 [P] Add an `authlimit` job to `.github/workflows/ci.yml` next to `billing-package`: checkout, `actions/setup-go` pinned by commit SHA exactly as the other actions are (look the SHA up with `gh api repos/actions/setup-go/git/ref/tags/<tag>`), `go vet`, `go test -race ./...` in `services/authlimit`, and `docker build services/authlimit`. Add the job to any required-checks list the repository keeps.
- [ ] T006 Settle the two open facts in research.md R2 against the real stack (`make up`, `make mcp-gateway-up`, `make mcp-gateway-bootstrap`): (a) decode one real `JWT-Standard` access token and write its full claim list into research.md R2, stating whether `iat` is present; if it is absent, try an application token format that includes it and record the result, and only then fall back to the documented estimate; (b) create a throwaway `Agent`/`MCP`-category application with scope `api:chat` and a public browser redirect to `https://api.localhost:8443/`, run authorization code + PKCE through the proxy and record whether it works. Delete the throwaway data afterwards.
- [X] T007 **HUMAN** Draft the constitution MINOR amendment 1.4.0 with `/speckit-constitution`: a rule for non-Python services in the style of "Self-contained packages" (own directory under `services/`, no import of repository code, the contract and shared vectors and JSON registries as the only coupling, a test that fails the build if crossed, own CI job, pinned images). Include the Sync Impact Report. The maintainer signs off in PR review; until then the plan's Complexity Tracking row stands.

**Checkpoint**: module builds (`make authlimit-build` fails only for the missing code), CI job exists, open facts recorded.

## Phase 2: Foundational (blocks every story)

- [X] T008 Implement `services/authlimit/internal/config/config.go` and `config_test.go`: read the variables of contracts/authlimit.md section E; required ones missing, or invalid, stop the start with an error naming the variable (never its value). Quote these rules in code comments and test them: `AUTHLIMIT_ISSUER` is `https` unless the host is loopback; `AUTHLIMIT_JWKS_URL` may be `http` for a loopback host or an in-network (dotless) name, else `https`; `AUTHLIMIT_IDENTITY_TTL_SECONDS` default 30 and at most 120; `AUTHLIMIT_MAX_TOKEN_LIFETIME` default 168h; `AUTHLIMIT_CLOCK_LEEWAY` default 10s; `AUTHLIMIT_JWKS_CACHE_SECONDS` 300, `_MIN_REFRESH_SECONDS` 30, `_FETCH_TIMEOUT_SECONDS` 5, `AUTHLIMIT_JWKS_STALE_GRACE_SECONDS` 21600; `AUTHLIMIT_REDIS_TIMEOUT_MS` 100; `AUTHLIMIT_LIMIT_MODE` is `enforce` or `observe` (default `enforce`); `AUTHLIMIT_REVOCATION_POLL_MS` 1000, `AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS` 30, `AUTHLIMIT_REVOCATION_UNAVAILABLE` is `closed` or `open` (default `closed`); `AUTHLIMIT_REQUIRED_SCOPE` default `api:chat`; tenant claim `owner`, principal claim `sub`.
- [X] T009 [P] Implement `services/authlimit/internal/errenv/errenv.go`, `services/authlimit/errorcodes.json` and tests: the `{"code","message","details"}` envelope and the codes `unauthenticated`, `forbidden`, `rate_limited`, `auth_unavailable`, `internal` with their HTTP statuses (401, 403, 429, 503, 500); fixed caller-facing messages; `details` limited to `required_scope` and `retry_after_seconds`; embed the JSON with `go:embed` and test that the Go constants and the file agree.
- [X] T010 [P] Implement `services/authlimit/internal/telemetry/telemetry.go` and `metrics.go`: `slog` JSON logging (fields: route class, outcome, reason, replica id; never a header or claim value) and every metric of research.md R14, defined in one file with the literal names (`agent_authlimit_requests_total`, `agent_authlimit_request_duration_seconds` with explicit buckets, `agent_authlimit_key_refresh_total`, `agent_authlimit_keys_age_seconds`, `agent_authlimit_revocation_age_seconds`, `agent_authlimit_store_errors_total`, `agent_authlimit_ready`, `agent_authlimit_signing_key_age_seconds`) so `tests/core/test_alert_rules.py` can read them. Export over OTLP/HTTP only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Test the label sets: no tenant, principal or token label exists.
- [X] T011 [P] Write `scripts/gen_auth_vectors.py` and generate `testdata/auth-vectors/jwks.json`, `vectors.json` and `README.md`: sign every case of research.md R18 once with a throwaway RSA key held only in memory (the private key is never written), record for each vector `{name, token, now, expected: {valid|refused, reason}}` with a fixed injected clock so none expires. Cover at least: valid; expired; not yet valid; missing `exp`, `sub`, `iat`; wrong issuer; wrong audience; client id as audience; `alg: none`; HS256 signed with the public key; unknown `kid`; foreign-key signature; lifetime over the cap (`exp − iat > 168h`); missing scope; tenant not text; principal not text; an internal identity presented as an external token. The README states how they were made and that no private key exists.
- [X] T012 Run the repository's Trivy secret scan on `testdata/auth-vectors/` (see how `make trivy` and the `trivy` CI job are invoked; use the pinned image, targeted at that directory only) and record the result in research.md R18. If it flags the token strings, add the narrowest allow rule the scanner supports, with a reason, and a test that fails if the rule outlives the directory; if it does not, say so. *(Result: Trivy's `jwt-token` rule flags the vectors at MEDIUM only; no gate reports MEDIUM, so no allow rule was added: research R18.)*
- [X] T013 [P] Implement `services/authlimit/internal/limiter/routes.go`, `services/authlimit/routes.json` and `routes_test.go`: ordered `{method, pattern, class}` entries, first match wins, `{param}` segments, classes `turn`, `upload`, `read`, `cancel`, `public`, `mcp`, each limited class carrying `tenant {capacity, per_minute}` and `person {capacity, per_minute}` (defaults: tenant capacity 10, 30 per minute; person capacity 5, 10 per minute); any unmatched path is `turn` with reason `unlisted_route`. Table content per research.md R11. Test every route listed there, an unlisted path, a trailing slash, a query string (ignored) and a method mismatch.
- [X] T014 Implement the process shell in `services/authlimit/cmd/authlimit/main.go` and `services/authlimit/internal/server/server.go`: subcommands `serve` (default), `healthcheck` (exit 0 if `/healthz` answers within 2 s), `keys`, `revoke` (stubs that exit with a clear message until their stories land); `GET /healthz` always 200; `GET /readyz` reports not-ready until the story tasks wire real checks; graceful shutdown on SIGTERM with a 10 s ceiling.
- [X] T015 [P] Implement `services/authlimit/cmd/echo/main.go`: a stub upstream that answers every request with JSON of the method, path, and every request header it received (this is how the smoke proves what reached the API); listens on `:8000`; `/health` and `/health/ready` return 200.

**Checkpoint**: `make authlimit-test` passes for config, errenv, telemetry, routes; vectors exist.

## Phase 3: User Story 1, a signed-in person's chat request carries a verified identity (P1) [service side]

**Goal**: Caddy asks the service; a valid token yields an internal identity that reaches the upstream with the right tenant and person; no `Authorization` header reaches the upstream.

**Independent test**: with the stack up, sign in as an `ecorp` user and call the echo through the front door: the echo shows `X-Internal-Identity` for `ecorp` and no `Authorization` (quickstart Q1, Q3).

- [X] T016 [US1] Implement the external token verifier in `services/authlimit/internal/extjwt/verify.go` with `verify_test.go`: algorithm taken from the allow-list `RS256`, never from the token (`none` and HMAC refused); `iss` equals `AUTHLIMIT_ISSUER` exactly; `aud` contains `AUTHLIMIT_AUDIENCE`; `exp` present and in the future within the leeway; `iat` present; `exp − iat ≤ AUTHLIMIT_MAX_TOKEN_LIFETIME`; `sub` present and text; the tenant claim present and text; the scope claim a space-separated string containing `AUTHLIMIT_REQUIRED_SCOPE`. Returns a typed result `{tenant, principal, scopes, iat, exp}` or a reason from research.md R14's list; never panics (a fuzz test feeds arbitrary bytes and JSON of every type into each claim). Run every vector in `testdata/auth-vectors/vectors.json` and require the expected outcome for all.
- [X] T017 [US1] Implement the key cache in `services/authlimit/internal/extjwt/jwks.go` with `jwks_test.go` (an `httptest` server): fetch with a `AUTHLIMIT_JWKS_FETCH_TIMEOUT_SECONDS` (5 s) deadline, parse RSA keys only (`kty RSA`, `use sig` or absent, `n`, `e`), cache for `AUTHLIMIT_JWKS_CACHE_SECONDS` (300), refresh on an unknown `kid` at most once per `AUTHLIMIT_JWKS_MIN_REFRESH_SECONDS` (30) however many tokens arrive, background refresh before expiry, bounded response size. Grace behaviour is added in T034; keep the age and last-attempt fields it needs.
- [X] T018 [US1] Implement the internal keyset in `services/authlimit/internal/identity/keyset.go` with tests: load `keyset.json` (`kid`, `publish_at`, `use_from`, `retire_at`) and `<kid>.pem` (PKCS#8 Ed25519) from `AUTHLIMIT_KEYS_DIR`; derive the state from the clock (published, current, retiring, retired) per data-model.md; sign with the newest key whose `use_from` has passed; publish every key whose `publish_at` has passed and `retire_at` has not; refuse to start with no current key; refuse a directory that is group- or world-readable.
- [X] T019 [US1] Implement identity minting and the public key endpoint in `services/authlimit/internal/identity/mint.go` and `internal/server/jwks.go` with tests: JWT `alg: EdDSA`, header `kid` and `typ: authlimit-identity+jwt`, claims `iss: urn:agent-core:authlimit`, `aud: agent-core-api`, `sub`, `tenant`, `scope`, `jti` (random), `iat`, `nbf`, `exp` with `exp − iat` equal to `AUTHLIMIT_IDENTITY_TTL_SECONDS` (default 30, at most 120); `GET /internal/jwks.json` serves the published public keys as an OKP/Ed25519 JWKS. Add a golden test that writes `testdata/auth-vectors/identity.json` (a minted token and the public JWKS only, throwaway key, discarded) with `-update-golden`, for PR 2's Python verifier.
- [X] T020 [US1] Implement `GET /v1/check` for the verified classes in `services/authlimit/internal/server/check.go` with tests: classify from `X-Forwarded-Method` and the path of `X-Forwarded-Uri` (T013), verify (T016), mint (T019), answer 200 with `X-Internal-Identity` for `turn`, `upload`, `read`, `cancel`; `public` answers 200 with nothing; 401 and 403 with the envelope and `WWW-Authenticate` exactly as the table in contracts/authlimit.md section A. The limiter and revocation hook points exist but are not yet called.
- [ ] T021 [P] [US1] Extend `scripts/mcp_gateway_bootstrap.py` and its tests (`tests/scripts/` or where the existing bootstrap tests live): an API application per tenant (scope `api:chat`, public client, redirect URI `https://api.localhost:8443/`, resource `https://api.localhost:8443`, token format `JWT-Standard`), idempotent like the rest; one `--token-hours` option and `TOKEN_HOURS` environment variable with default **168** for every application it creates (this replaces the constant `TOKEN_HOURS = 1`); update the printed instructions. Keep the refresh lifetime at 168. *(Also: a `--token-hours` option and TOKEN_HOURS variable; tests in `tests/scripts/test_mcp_gateway_bootstrap.py`.)*
- [ ] T022 [US1] Add `authlimit` (×2 via `deploy.replicas: 2`) and `authlimit-echo` to `deploy/compose/docker-compose.yml` under profile `gateway`: hardened like `mcp-gateway` (read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, non-root), environment from contracts/authlimit.md E (issuer and JWKS URL as `mcp-gateway` uses; audience `https://api.localhost:8443`; Redis URL of the existing `redis` service), the key directory mounted read-only, healthcheck `["CMD", "/authlimit", "healthcheck"]`, `depends_on` redis and casdoor healthy. Interpolation must work with nothing set (`tests/core/test_compose_gateway.py` is the model; a required variable in an opt-in profile breaks every other compose command, ENGINEERING_PATTERNS 83).
- [ ] T023 [US1] Add the `api` site to `deploy/caddy/Caddyfile.gateway` per contracts/authlimit.md A: public matcher straight to the upstream; the default handler with `forward_auth` (`dynamic a` over `authlimit` port 8080, `uri /v1/check`, `copy_headers X-Internal-Identity`, `lb_try_duration 5s`, `lb_try_interval 250ms`), then `request_header -Authorization`, then `reverse_proxy` with `flush_interval -1`; the upstream is `authlimit-echo:8000` in this PR (PR 2 repoints it at the API). Mount the file as the existing `caddy-gateway` service does. Extend `tests/core/test_compose_gateway.py` (or add `tests/core/test_authlimit_caddyfile.py`) to assert: `request_header -Authorization` follows `forward_auth` on the verified handler; `copy_headers X-Internal-Identity` is present; the `/internal/jwks.json` path is not routed.
- [X] T024 [US1] Implement `authlimit keys init|rotate|list` in `services/authlimit/cmd/authlimit/keys.go` with tests: `init` creates the directory with mode 700 and the first key (`publish_at = use_from = now`), never overwriting; `rotate --overlap` (default 10m) adds a key with `publish_at = now`, `use_from = now + overlap`, and sets the previous key's `retire_at = use_from + overlap`; `list` prints `kid`, state and times only, never key material. Add Makefile targets `authlimit-keys` (directory outside the repo with the `mcp-gateway-secrets` convention, mode 700) and `authlimit-up` / `authlimit-down` (the way `mcp-gateway-up` / `-down` are written).
- [ ] T025 [US1] Write `scripts/authlimit_smoke.py` (model: `scripts/mcp_gateway_smoke.py`; reuse its OAuth helpers by import if they are importable, else copy minimally) with Q1, Q2 (no token, tampered, expired, wrong audience, MCP-audience token) and Q3 (forged `X-Tenant-Id`, `X-Principal-Id`, `X-Internal-Identity` once, twice and in lower case) against the echo through `https://api.localhost:8443`; it creates throwaway users and deletes them. Add `make authlimit-smoke`. Run it on the real stack and record the outcome in research.md.

**Checkpoint**: US1 service side works on the real stack against the echo.

## Phase 4: User Story 2, bad tokens are stopped at the front door (P1)

**Goal**: every refusal reason from FR-002 is refused with the envelope, nothing reaches the upstream.

**Independent test**: all shared vectors get their expected outcome; the smoke's Q2 passes.

- [X] T026 [US2] Write `services/authlimit/internal/server/check_refusals_test.go`: for every vector in `testdata/auth-vectors/vectors.json`, call `/v1/check` through the real handler and require the status, `WWW-Authenticate` and envelope code the contract gives; assert the upstream-side contract (no `X-Internal-Identity` on any refusal) and that the response body never contains any part of the token.
- [X] T027 [US2] Add the lifetime-cap and no-issue-time refusals end to end: tests for `exp − iat > AUTHLIMIT_MAX_TOKEN_LIFETIME` (refused with reason `lifetime_too_long`) and a token without `iat` (reason `no_issue_time`), plus a test that the cap is configurable (lower value refuses a 168h token).
- [ ] T028 [US2] Run the mutation list by hand against the verifier and record each result in research.md (a section "Mutations"): remove the audience check; remove the issuer check; take the algorithm from the token; drop the lifetime cap; accept a missing `iat`; accept an internal identity as an external token; skip the scope check. Each mutation must make at least one test fail; revert each after. A mutation that survives is a missing test: add it. *(Built as `scripts/authlimit_mutants.py` / `make authlimit-mutants`, 26 defects, 26 caught after one new test: research R21.)*

## Phase 5: User Story 3, fair and visible rate limits (P2)

**Goal**: per-tenant and per-person token buckets in Redis; 429 with `Retry-After`; cancel and reads never limited; observation mode.

**Independent test**: quickstart Q4 and Q5.

- [X] T029 [US3] Implement the bucket script `services/authlimit/internal/limiter/bucket.lua` and `limiter.go` with `limiter_test.go` (miniredis): one atomic call that reads Redis `TIME`, refills both buckets (`tokens = min(capacity, tokens + elapsed × rate)` in integer millitokens), consumes one from each only if **both** have at least one, otherwise none, and returns the wait until the emptier bucket has a token; `Retry-After` is that wait rounded up to whole seconds, minimum 1; keys `authlimit:v1:b:{<tenant>}:t:<class>` and `authlimit:v1:b:{<tenant>}:p:<sub>:<class>` (hash tag braces), expiring after twice the time to fill from empty. Tests: starts full; refills; capacity cap; two-bucket atomicity (a denied person does not consume the tenant's token); different tenants independent; two clients sharing the store count once. If miniredis cannot run the script, pass `now` in from Go as research.md R4 allows and say so in a comment.
- [X] T030 [US3] Add the call ceiling and failure policy to the limiter: every Redis call has the `AUTHLIMIT_REDIS_TIMEOUT_MS` (100 ms) deadline; an error or timeout serves the request (fail open), increments `agent_authlimit_store_errors_total{store="limiter"}` and logs once per interval; test with a store that errors and one that hangs.
- [X] T031 [US3] Wire the limiter into `/v1/check` for `turn` and `upload` classes only; `read` and `cancel` skip it entirely (test that a throttled tenant can still cancel and read); modes: `enforce` answers 429 with `Retry-After` and the envelope; `observe` serves, increments `outcome="would_limit"` and logs the would-be refusal. Test both modes and that a refused-by-token request never reaches Redis (use a store that fails the test if called).
- [ ] T032 [US3] Add smoke scenarios Q4 and Q5 to `scripts/authlimit_smoke.py` (two tenants, drive one past its limit, cancel still served, then observe mode via a compose override or environment and no 429); run on the real stack and record in research.md.

## Phase 6: User Story 4, the front door survives the failures it will meet (P2)

**Goal**: replica loss, internal key rotation, Casdoor outage inside and beyond a grace.

**Independent test**: quickstart Q6, Q7, Q8.

- [X] T033 [US4] Implement readiness in `services/authlimit/internal/server/ready.go`: `/readyz` is 503 unless there is a current signing key and the external keys are usable (fresh, or stale within the grace); liveness `/healthz` never depends on Casdoor or Redis; `agent_authlimit_ready` follows readiness; the replica reports `degraded` in logs while stale-within-grace.
- [X] T034 [US4] Implement the stale-key grace in `services/authlimit/internal/extjwt/jwks.go`: when a refresh fails keep using the cached keys while `age ≤ AUTHLIMIT_JWKS_STALE_GRACE_SECONDS` (21600); count `key_refresh_total{result="error"}` on every failure; set `agent_authlimit_keys_age_seconds`; beyond the grace every token is refused with reason `keys_unavailable` and status 503 `auth_unavailable` with `Retry-After: 30`. Tests with an injected clock for fresh, stale-within-grace, unusable, and recovery on the next successful fetch.
- [X] T035 [US4] Test key rotation in `services/authlimit/internal/identity/rotation_test.go` with an injected clock: before `use_from` the old key signs; after, the new key signs; both are published through the overlap; the old key disappears at `retire_at`; an identity minted under the old key still verifies (against the published set) during the overlap; a replica restarted mid-rotation chooses the same current key as its peer.
- [ ] T036 [US4] Add smoke scenarios Q6 (kill one replica under steady traffic: count failures and require none beyond in-flight), Q7 (stop `casdoor` with the grace set to 30 s through the smoke's compose override: existing token works inside, 503 beyond, recovery within one refresh interval) and Q8 (`keys rotate --overlap 20s` under traffic: zero failures, new `kid` published). Run on the real stack; record numbers in research.md.
- [ ] T037 [US4] Measure the added latency (SC-008): a script `scripts/authlimit_latency.py` that sends a fixed number of authenticated requests through the front door to the echo and directly to the echo, and reports median and 95th percentile of the difference. Run it on the local stack, record the numbers in research.md R6, and state plainly whether the proposed targets (median under 10 ms, 95th under 50 ms) were met.

## Phase 7: User Story 5, the MCP routes share limits and revocation (P2)

**Goal**: limit-only mode on `/ecorp/mcp` and `/ops/mcp`; `mcp-gateway` keeps answering its own 401.

**Independent test**: quickstart Q10.

- [X] T038 [US5] Implement the `mcp` class in `services/authlimit/internal/server/check_mcp.go` with tests: verify signature, issuer and expiry only (not audience or scope); valid and not revoked: apply the buckets, answer 200 with **no** identity header, or 429 when limited; missing or invalid token: answer 200, limit nothing; revoked (once T043 lands; leave the hook): 401 with `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<template>"` built from `AUTHLIMIT_MCP_METADATA_URL_TEMPLATE`, required when `mcp` routes exist.
- [ ] T039 [US5] Add the `mcp` handler to the `api` site of `deploy/caddy/Caddyfile.gateway` (paths `/ecorp/mcp`, `/ops/mcp`): `forward_auth` without `copy_headers`, then `reverse_proxy mcp-gateway:8000` **without** removing `Authorization`; the `/.well-known/oauth-protected-resource/*` paths go to `mcp-gateway` without `forward_auth`. Set `AUTHLIMIT_MCP_METADATA_URL_TEMPLATE` in the compose service (T022). Extend the Caddyfile test of T023 to assert `Authorization` is not stripped on the MCP handler. *(Built on the existing MCP host's site, `mcp.localhost`, not on the `api` site: that is the host MCP clients use and where tokens' audience is already pinned. The discovery paths skip the check.)*
- [ ] T040 [US5] Add smoke scenario Q10 (valid token until 429; no token reaches `mcp-gateway`'s own 401 with its metadata) against the real `mcp-gateway`; run and record.

## Phase 8: User Story 6, revocation and bounded token lifetime (P3)

**Goal**: an operator cuts off a person within 5 seconds on every replica; a fresh sign-in works.

**Independent test**: quickstart Q9.

- [X] T041 [US6] Implement `services/authlimit/internal/revoke/revoke.go` with tests (miniredis): `Add(tenant, sub, at)` into the sorted set `authlimit:v1:revoked` (member `<tenant>\x00<sub>`, score Unix seconds), trimming entries older than `AUTHLIMIT_MAX_TOKEN_LIFETIME`; a poller that loads the whole set every `AUTHLIMIT_REVOCATION_POLL_MS` (1000) into memory; `Revoked(tenant, sub, iat)` is a map lookup with the rule **token `iat ≤ revoked_at` is void**; a documented bound of 10,000 entries, exceeding it logged.
- [X] T042 [US6] Implement the staleness policy in the same package: if the poll fails keep enforcing the last copy until it is older than `AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS` (30); after that `closed` (default) refuses with reason `revocation_unavailable` and status 503 `auth_unavailable`, `open` serves; set `agent_authlimit_revocation_age_seconds` and `store_errors_total{store="revocation"}`. Tests with an injected clock and a store that fails.
- [X] T043 [US6] Wire revocation into `/v1/check` **before** the limiter on every verified class and into the `mcp` class (T038's hook): a revoked person gets 401 and no bucket is touched (a test uses a store that fails if called). Implement `authlimit revoke --tenant T --sub S` and `authlimit revoke --list` in `cmd/authlimit/revoke.go`; add `make authlimit-revoke TENANT= SUB=` running it in a replica (`docker compose exec`).
- [ ] T044 [US6] Add smoke scenario Q9 (sign in, call, revoke, both replicas refuse within 5 s on a chat route and on `/ecorp/mcp`, sign in again, call succeeds); run and record the measured time in research.md.

## Phase 9: User Story 7, operators can see and trust the service (P3)

**Goal**: every degrade path has a metric and an alert; no secret in any log; the threat model reviewed.

**Independent test**: quickstart Q11 and the alert-rule test.

- [X] T045 [US7] Add the alert rules to `observability/prometheus/alerts.yml` in the style of `McpGatewayCannotCheckTokens`: `AuthlimitCannotCheckTokens` (`increase(agent_authlimit_requests_total{reason="keys_unavailable"}[10m]) > 0` for 5m), `AuthlimitKeysStale` (`agent_authlimit_keys_age_seconds` above the cache TTL for 5m), `AuthlimitLimiterStoreDown` (`increase(agent_authlimit_store_errors_total{store="limiter"}[10m]) > 0`), `AuthlimitRevocationStale`, `AuthlimitReplicasDown` (`sum(agent_authlimit_ready) < 2` for 5m), `AuthlimitLimiterObserveOnly` (info). Each annotation says what to check and why the alert exists.
- [X] T046 [US7] Extend `tests/core/test_alert_rules.py` so `_defined_metrics()` also reads the metric names from `services/authlimit/internal/telemetry/metrics.go`, and add tests that each new alert exists and that an alert naming a metric the Go file does not define fails. Run only this test file locally.
- [ ] T047 [US7] Add the secret-leak check to the smoke (Q11): after the run, collect every `authlimit`, `authlimit-echo`, `caddy-gateway` and `casdoor` log line and search for the access token, the refresh token and any internal private key bytes; the run fails on a match. Record the result.
- [X] T048 [US7] Add `tests/core/test_authlimit_boundary.py`: the service imports nothing from the repository (no Go import path outside its module, no reference to `app/` in `services/authlimit/`), and nothing under `app/` or `packages/` references `services/authlimit` except the documented JSON registries and vectors. Mirror the shape of `tests/billing/test_billing_core_boundary.py`.
- [ ] T049 [US7] Walk the code review checklist at the end of threat-model.md against the finished Go code and fix or record each finding in threat-model.md "Review notes": no unreturned reason, no `recover` hiding a bug, no algorithm taken from the token, no log call taking a header or claim value, every outbound call with a deadline.

## Phase 10: PR 1 polish and shipping

- [ ] T050 [P] Write `services/authlimit/README.md` (what it is, the commands, the settings pointer, how to run the tests) and add a `services/authlimit` row to CLAUDE.md "Where things live" and the README architecture section; update CLAUDE.md's spec sentence if numbers moved.
- [ ] T051 [P] Add the numbered pattern entries (take the next free numbers at write time; do not reuse): the gateway layer pattern (verify at the edge, pass a signed internal identity, never trust a header) and the revocation-copy pattern, each with the failure that motivated it, to `GRAPH_PATTERNS.md` or `ENGINEERING_PATTERNS.md` per the constitution's routing rule; update the "Extending Further" entry that spec 017 left for per-tenant limits.
- [ ] T052 Run the targeted checks locally and fix findings: `make authlimit-test`; `pytest tests/core/test_alert_rules.py tests/core/test_authlimit_boundary.py tests/core/test_compose_gateway.py -q` plus the bootstrap script's tests; `ruff check` on the touched Python files; `make trivy` only if the Docker setup allows without strain, else leave to CI and say so.
- [ ] T053 Count the hand-written lines of each of the three pull requests (excluding `go.sum`, generated vectors, lock files) and write the counts, the per-area split and why the layers could not be smaller into research.md "Size"; each description says in its first paragraph that it is over the repository's ~1,000 ceiling and by how much.
- [ ] T054 Commit Part A as the three pull requests above (conventional `feat:` subjects; the body states what the change does and what is left undone; end with the attribution line the session was given), push the branches, and open 1a against `main` and 1b and 1c each against the one before (CI runs only for pull requests whose base is `main`, so 1b and 1c get theirs when they are restacked onto `main` after the one before merges). The descriptions are built from spec.md "Why", the decisions, the measured results, the size note, the unresolved HUMAN items (T007), and a test plan. After opening, report CI: run `gh pr checks <n>`, do not call it done while checks are pending or red, and separate failures also red on `main` from new ones.

**Checkpoint (PR 1 done)**: the service runs as two replicas behind Caddy in the local stack with Casdoor and Redis, proven against the echo and the real `mcp-gateway`.

---

# PART B: PR 2, the Python side

Start from `main` after PR 1 merges (or stack on PR 1's branch and restack after, per the repository's restack recipe).

## Phase 11: API foundation (blocks the rest of Part B)

- [ ] T055 [US1] Add `AUTH_MODE` (`identity` default, or `headers`) and `AUTHLIMIT_KEYS_URL` (required in identity mode) and `AUTHLIMIT_KEYS_CACHE_SECONDS` (default 60) to `Settings` in `app/core/config.py`, with entries in `.env.example` (documenting `AUTH_MODE=headers` for running the API alone) and `deploy/env/prod.env.example`. Identity mode without the keys URL raises at API startup with a message naming the variable. Test in `tests/core/test_config_auth_mode.py`.
- [ ] T056 [P] [US2] Add the new `ErrorCode` values (`unauthenticated`, `forbidden`, `rate_limited`, `auth_unavailable`) to `app/core/errors.py` and a test `tests/core/test_error_codes_authlimit.py` that fails if the registry and `services/authlimit/errorcodes.json` disagree.
- [ ] T057 [US1] Implement `app/core/internal_identity.py` (imports nothing from `agent`, `domains` or `api`) with `tests/core/test_internal_identity.py`: verify the compact JWS with PyJWT: `algorithms=["EdDSA"]` only, header `typ` equal to `authlimit-identity+jwt`, `iss` equal to `urn:agent-core:authlimit`, `aud` equal to `agent-core-api`, `exp` and `nbf` enforced with a small leeway, `tenant` and `sub` present and non-empty text; a public-key cache fetched from `AUTHLIMIT_KEYS_URL` over `httpx` with a 5 s deadline, kept `AUTHLIMIT_KEYS_CACHE_SECONDS` (60), refreshed on an unknown `kid` at most once per 10 s; never raises (every failure is a `None` plus a counted reason). Run it against `testdata/auth-vectors/identity.json` and against tokens minted in the test for each refusal reason (wrong audience, wrong issuer, wrong `typ`, foreign key, expired, an external RS256 token presented as an identity).

## Phase 12: User Story 1, the API runs as the verified person (P1) [API side]

- [ ] T058 [US1] Change `get_ctx` in `app/api/deps.py` by mode: identity mode reads `X-Internal-Identity`, verifies it (T057) and returns `{"tenant", "principal", "claims": {}}` from its `tenant` and `sub` only, ignoring `X-Tenant-Id` and `X-Principal-Id`; a missing or invalid identity is HTTP 401 with the `unauthenticated` envelope, never a default tenant; header mode keeps today's behaviour. Update the module docstring (it describes a gateway that did not exist). Tests in `tests/api/test_deps_auth_modes.py`: forged headers ignored in identity mode; missing identity is 401; header mode unchanged.
- [ ] T059 [US1] Make the hermetic suite select header mode explicitly: an autouse fixture in `tests/conftest.py` sets `AUTH_MODE=headers` (and clears the settings cache the repo uses) for every existing test; a new `identity_mode` fixture opts a test back in. Run `pytest tests/api -q` to confirm the existing API tests pass unchanged, then leave the full suite to CI.
- [ ] T060 [US1] Announce header mode: at startup in header mode log a warning line (metadata only) and set the gauge `agent_api_auth_mode{mode}` defined in `app/core/metrics.py`; add its test in `tests/core/test_metrics.py` style.
- [ ] T061 [US1] Repoint the `api` site in `deploy/caddy/Caddyfile.gateway` from `authlimit-echo` to the `api` service; add the `api` service wiring for identity mode (`AUTH_MODE: identity`, `AUTHLIMIT_KEYS_URL: http://authlimit:8080/internal/jwks.json`) under the profile combination the dev stack uses (`gateway` with `app`); add `make authlimit-up-app`. Extend `tests/core/test_compose_gateway.py` accordingly.
- [ ] T062 [US1] Web UI sign-in in `app/api/static/` (new `auth.js` loaded by `index.html`, public in the Caddy matcher): authorization code with PKCE against Casdoor as a public client, redirect to the front-door origin, token in `sessionStorage`, `Authorization: Bearer` on every `fetch` (the UI streams with `fetch`, not `EventSource`), the tenant selector hidden and its value never sent in identity mode, sign-in again on a 401. Keep header-mode behaviour (selector shown) when the page is served by an API in header mode (the page reads a small `/auth/config`-style indicator or a meta tag the system router renders; choose the smaller one and document it). No third-party script; no token in a URL.

## Phase 13: User Story 3, one rate-limit policy point (P2) [API side]

- [ ] T063 [US3] **Remove** the in-API per-tenant request limiter (maintainer's decision, FR-029): in `app/api/rate_limit.py` delete the per-tenant moving-window limiter, `RATE_LIMITED_PATHS`, `_tenant_key` and the tenant branch of `TenantRateLimitMiddleware`, keeping only the payment-webhook per-address limit (rename the middleware to what it now is, e.g. `WebhookRateLimitMiddleware`, and update `app/api/main.py`); delete the `RATE_LIMIT_PER_MINUTE` setting from `app/core/config.py`, `.env.example`, `deploy/compose/docker-compose.prod.yml` and `deploy/env/prod.env.example`, and the metric `agent_rate_limit_exceeded_total` only if nothing else uses it (otherwise keep it for the webhook and say so); rewrite `tests/api/test_rate_limit.py` to cover the webhook limit only and add a test that a burst of turn-creating requests is never throttled by the API itself; remove or repoint the alert on tenant rate-limit spikes in `observability/prometheus/alerts.yml` and its test; update spec 004's FR-027 note, the README "Limits" line, GRAPH_PATTERNS pattern 43 (the multi-process limiter lesson, now historical: say where the limit lives now), `tests/core/test_env_example_matches_settings.py` expectations, and any dashboard panel that charts the removed metric.
- [ ] T064 [US3] Add the route-table drift test `tests/api/test_authlimit_routes_cover_api.py`: build the FastAPI app, collect every route (path and method) including the billing router, and require each to be matched by `services/authlimit/routes.json` with the class the contract expects (the three limited routes are `turn`/`upload`, `POST /chat/cancel` is `cancel`, health, page and webhook are `public`); fail with the list of unlisted routes.

## Phase 14: User Story 2 and 5, one set of vectors for both verifiers (P1/P2)

- [ ] T065 [US2] Run the shared external-token vectors against the MCP gateway's verifier: `tests/mcp/test_gateway_vectors.py` loads `testdata/auth-vectors/{jwks.json,vectors.json}` and requires `app/mcp/gateway_auth.py` to reach the same valid/refused verdict as the vector says for every vector the MCP verifier is responsible for (it does not check scope or lifetime cap: mark those vectors `applies_to: ["authlimit"]` in the file if needed and document it in the README). Any disagreement is a bug in one verifier: fix the verifier or the vector, and record which.
- [ ] T066 [US5] If T065 finds the MCP verifier more permissive than the service on a vector that matters (for example a missing issue time), decide per case: fix `app/mcp/gateway_auth.py` with a regression test that fails without the fix, or document the difference in `contracts/authlimit.md`. Add a GRAPH_PATTERNS note if behaviour changes.

## Phase 15: User Story 7, header mode is visible (P3) [API side]

- [ ] T067 [US7] Add alert `ApiHeaderAuthMode` to `observability/prometheus/alerts.yml` (`agent_api_auth_mode{mode="headers"} == 1` for 10m, severity warning, annotation explains that header mode trusts client headers and must never face the internet) and its test in `tests/core/test_alert_rules.py`.

## Phase 16: End-to-end smoke with the real API (all stories)

- [ ] T068 Extend `scripts/authlimit_smoke.py` for the real API (`--upstream api`): Q1 to Q11 against the real `api`, plus a two-tenant isolation check (an `ecorp` and an `other-co` user each ask for stored rows through the real tools and see only their own: the live equivalent of spec 017's check) and a check that a request carrying a forged `X-Tenant-Id` runs as the token's tenant. Run it on the real stack with `AUTH_MODE=identity`; record results in research.md. Run the web UI sign-in once by hand (or with the repo's Playwright setup if it fits without strain) and record what happened.

## Phase 17: Production wiring (cuttable first, per the spec's Delivery section)

- [ ] T069 [P] Wire `deploy/compose/docker-compose.prod.yml` and `deploy/caddy/Caddyfile` for identity mode: `authlimit` ×2 with required variables (no defaults for issuer, JWKS URL, audience, Redis URL, keys directory), `forward_auth` and `request_header -Authorization` on the API site, the API with `AUTH_MODE: identity` and `AUTHLIMIT_KEYS_URL`. Document in `deploy/env/prod.env.example` that no identity provider is provisioned by this feature (spec 017 T021) and that without one the API refuses requests. Extend the prod compose tests the repo has (look for `test_compose_prod*`) so the new service and required variables are checked.

## Phase 18: PR 2 polish and shipping

- [ ] T070 [P] Update docs: `README.md` (architecture, make targets, the sign-in and header-mode notes), `CLAUDE.md` (where things live: `services/authlimit/`, `app/core/internal_identity.py`), the numbered patterns for the API-side lessons (take next free numbers), `specs/018-.../research.md` "Results" with the measured numbers, and every "Extending Further" / Roadmap entry the change closes or opens (the in-flight turn cap).
- [ ] T071 Run the targeted Python checks and fix findings: `pytest tests/core/test_config_auth_mode.py tests/core/test_error_codes_authlimit.py tests/core/test_internal_identity.py tests/api tests/mcp/test_gateway_vectors.py tests/core/test_alert_rules.py tests/core/test_compose_gateway.py -q`; `ruff check .` on touched files; `mypy` on the touched modules if it runs quickly, else leave to CI and say so. Do not run the full suite locally.
- [ ] T072 Count PR 2's hand-written lines as in T053 and record them; name the pieces split off first (production wiring T069, then refresh handling in the UI).
- [ ] T073 Commit PR 2 (`feat:` subject; body states failure mode, root cause, what is left undone, and the `.env` note from spec A9; attribution line), push, open the PR against `main` (or against PR 1's branch if stacked, and say so), report CI with `gh pr checks`, and do not call it done while checks are pending or red.

---

## Dependencies and execution order

- Phase 1 → Phase 2 → stories. T006 (real token facts) gates T016 (verifier, `iat`) and T021 (application type).
- Within Part A: US1 (Phase 3) is the MVP and gates everything. US2 (4) needs only the verifier. US3 (5) needs `/v1/check` (T020) and routes (T013). US4 (6) needs the key cache and keyset (T017, T018). US5 (7) needs the limiter (T029) and the Caddy `api` site (T023). US6 (8) needs `/v1/check` and the limiter (T031) for ordering. US7 (9) needs the metrics (T010) and the stories' failure paths.
- Part B starts after Part A is merged or stacked. T057 gates T058; T058 and T059 gate every Part B test that touches the API; T065 needs only the vectors from Part A.
- T007 (constitution) is HUMAN and can run in parallel; it must be settled or recorded as a Complexity Tracking exception before PR 1 merges.

## Parallel opportunities

- Phase 1: T003, T004, T005 together.
- Phase 2: T009, T010, T011, T013, T015 are independent files; T008 first.
- Phase 3: T021 (Python bootstrap) is independent of the Go tasks; T018 and T016/T017 can proceed together; T019 needs T018.
- Phase 9: T045 and T048 in parallel.
- Part B: T056 beside T055; T063 and T064 beside T062; T067 and T069 are independent.

## Implementation strategy

- **MVP**: Phases 1 to 3 (US1, service side): a valid token yields an internal identity at the echo. Stop and verify with the smoke before adding anything else.
- **Then** US2 (cheap, mostly tests), US3, US4, US6, US5, US7, in that order of risk, running the smoke after each.
- **PR 1 ships at the end of Phase 10.** Part B follows; the API cannot be in identity mode before PR 1's keys endpoint exists.
- If a PR must shrink, cut in the order the spec names: PR 1 loses `authlimit revoke` (T043's command) and the constitution amendment text; PR 2 loses production wiring (T069), then refresh handling in the UI.

## Task count

73 tasks: Part A 54 (setup 7, foundational 8, US1 10, US2 3, US3 4, US4 5, US5 3, US6 4, US7 5, polish 5), Part B 19 (foundation 3, US1 5, US3 2, US2/US5 2, US7 1, smoke 1, production 1, polish 4).
