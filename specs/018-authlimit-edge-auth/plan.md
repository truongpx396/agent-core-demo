# Implementation Plan: Edge authentication and rate limiting (authlimit)

**Branch**: `feat/018-authlimit-edge-auth` | **Date**: 2026-10-27 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/018-authlimit-edge-auth/spec.md`

## Summary

Put one verified-identity and rate-limit policy point in front of the chat API and the MCP routes. Caddy asks a small Go service (`authlimit`, two replicas) about each request through `forward_auth`. The service verifies the Casdoor access token offline, enforces per-tenant and per-person token buckets in the existing Redis, refuses revoked people, and returns a short-lived, asymmetrically signed internal identity that Caddy copies onto the request after removing the caller's `Authorization` header. The Python API trusts only that identity (identity mode is the default; header mode stays as an explicit opt-in for tests and direct local runs). `mcp-gateway` keeps verifying the external token itself; its routes pass through the service in limit-only mode, which closes spec 017's T023 with the same limiter and revocation.

The approach rests on measurements, not documentation: on Caddy 2.11.7 the `Authorization` header reaches the auth service, a forged identity header never survives, 401 and 429 pass through with their headers, `lb_try_duration` and `dynamic a` work inside `forward_auth`, and an unreachable auth service gives a 502 with the upstream untouched (`research.md` R1). Delivery is two PRs (PR 1 the service on its own, PR 2 the Python side), which will exceed the repository's size ceiling and say so.

## Technical Context

**Language/Version**: Go (the pinned `golang:1.26` build image; `go.mod` states the language version) for the service; Python 3.13 for the API side (unchanged).

**Primary Dependencies**: Go: standard library `net/http`, `log/slog`, `crypto/ed25519`, `crypto/rsa`; `github.com/golang-jwt/jwt/v5` (v5.3.1), `github.com/redis/go-redis/v9` (v9.23.0), the OpenTelemetry Go SDK with the OTLP/HTTP metric exporter (so metrics reach the same collector as the app's and `mcp-gateway`'s); tests: `github.com/alicebob/miniredis/v2` (v2.40.0). Python: PyJWT 2.14 with `cryptography` (both already installed; EdDSA verification exercised in `research.md` R5), `httpx`.

**Storage**: the existing Redis (own key prefix `authlimit:v1:`): token-bucket state, the revocation list. Key material is a mounted read-only directory (a file-backed keyset), never Redis.

**Testing**: `go test -race ./...` hermetic (miniredis, an in-memory JWKS server, an injected clock); shared token vectors run by Go and Python; `scripts/authlimit_smoke.py` against the real stack (Casdoor, Caddy, Redis, two replicas), a manual tier like `mcp-gateway-smoke`; Python default suite stays hermetic.

**Target Platform**: Linux containers (distroless static, non-root, read-only root filesystem) under the repository's compose files; the host for development is macOS with Docker.

**Project Type**: a self-contained service plus changes to the existing web service (API, static UI) and the deployment files.

**Performance Goals**: median added time per authenticated request under 10 ms and 95th percentile under 50 ms on the local stack (SC-008: proposals, to be measured). Reference point: PyJWT verifies one RS256 token in about 55 µs on one core (microbenchmark, `research.md` R6).

**Constraints**: stateless apart from caches; every wait bounded (key fetch 5 s, store call 100 ms, grace, identity lifetime); no token, key or claim value in logs, labels or error bodies; the service fails closed on verification and fails open on the limiter store; two replicas by default; no Cloudflare and no per-address limits (later).

**Scale/Scope**: sized for the local stack and a single production host (hundreds of requests per second, not thousands). Not a claim about larger scale, which has not been measured.

## Constitution Check

*GATE: passed before Phase 0 and re-checked after Phase 1 design. One justified deviation (Runtime: Python 3.13), recorded below.*

| Principle | Touched? | How the design meets it |
|---|---|---|
| I. Fail-closed tenant isolation (non-negotiable) | Yes | `SecurityCtx` is stamped once, in the API's single identity dependency, from a verified internal identity (FR-010). Missing, expired or malformed identity gives 401, never a default tenant. The tenant and principal come only from verified claims (FR-004). The store-level `WHERE tenant = %s` is untouched. A two-tenant end-to-end check proves isolation through the front door (SC-002). |
| II. Mandatory approval for side effects | No | No tool is added or changed. |
| III. Fixed, typed tools | No | No tool surface changes. |
| IV. Exactly-once side effects | No | No side-effect tool. The only retry is Caddy re-dialling another replica after a failed *connect* (measured R1), before any request is delivered. |
| V. Bounded, observable failure | Yes | Failure-policy table below; every degrade path has a metric and an alert (FR-031); every wait has a ceiling; logs are metadata only (FR-032); errors use the `ErrorCode` envelope (FR-033). |
| VI. Untrusted content is data | Yes | The bearer token and all client headers are untrusted input, verified first, never logged; the external token is stripped before the API (FR-037). |
| VII. Test discipline | Yes | Python default suite hermetic and unchanged apart from fixtures selecting header mode; Go suite hermetic; the live behaviour is proven by the manual smoke; a mutation list for the verifier and limiter is run by hand and recorded, as in spec 017. |
| VIII. Why-first documentation | Yes | Contract, research, threat model, numbered patterns (numbers taken at write time, never reused), README and CLAUDE.md entries; honest gaps listed. |
| Architecture: Runtime Python 3.13 | **Deviation** | The Go service is outside the line "Runtime: Python 3.13". A MINOR amendment (1.4.0) adding a non-Python-services rule is drafted in PR 1 through `/speckit-constitution` and needs the maintainer's sign-off (HUMAN). Until it lands the deviation is in Complexity Tracking. |
| Architecture: configuration | Yes | New API settings go through `Settings` with `.env.example` entries; the service's own settings are environment variables validated at start, documented in the contract. |
| Architecture: observability | Yes | Metrics over OTLP like the app and `mcp-gateway`; names carry the `agent_` prefix so `alerts.yml` and its drift test treat them alike. |
| Workflow: Spec Kit gates | Yes | A store consumer states its failure policy and the metric/alert that covers it: see the table. |

### Failure policies (Principle V)

| Dependency | Failure | Policy | Metric (outcome / reason) | Alert |
|---|---|---|---|---|
| Casdoor keys, fresh | cannot refresh | serve from last keys | `agent_authlimit_keys_age_seconds`, `agent_authlimit_key_refresh_total{result="error"}` | `AuthlimitKeysStale` after 5 min |
| Casdoor keys, past grace (6 h) | no usable key | refuse every request (fail closed), not ready | `agent_authlimit_requests_total{outcome="refused",reason="keys_unavailable"}` | `AuthlimitCannotCheckTokens` |
| Redis, buckets | error or over 100 ms | serve (fail open) | `agent_authlimit_store_errors_total{store="limiter"}` | `AuthlimitLimiterStoreDown` |
| Redis, revocation list | sync fails | enforce last copy up to 30 s, then refuse (configurable) | `agent_authlimit_revocation_age_seconds`, `agent_authlimit_store_errors_total{store="revocation"}` | `AuthlimitRevocationStale` |
| Signing key | missing or unreadable | do not start; not ready | `agent_authlimit_ready` | `AuthlimitReplicasDown` |
| A replica | stops | Caddy retries another replica on a failed connect | `agent_authlimit_ready` (one series per replica) | `AuthlimitReplicasDown` (fewer than 2 ready for 5 min) |
| API in header mode | left on | serve, loudly | `agent_api_auth_mode{mode="headers"}` | `ApiHeaderAuthMode` (PR 2) |

## Project Structure

### Documentation (this feature)

```text
specs/018-authlimit-edge-auth/
├── spec.md
├── plan.md              # this file
├── research.md          # measured behaviour, decisions, alternatives
├── data-model.md        # entities, validation, state transitions
├── threat-model.md      # FR-035
├── quickstart.md        # runnable validation scenarios
├── contracts/
│   └── authlimit.md     # wire contract, identity format, Redis keys, commands, settings
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source code (repository root)

```text
services/authlimit/                      # PR 1: self-contained Go module, own tests and image
├── go.mod / go.sum
├── Dockerfile                           # build stage pinned by digest; distroless static, non-root; /authlimit and /echo
├── README.md
├── routes.json                          # route classes (embedded; overridable by file); read by a Python drift test in PR 2
├── errorcodes.json                      # the error codes the service emits; read by a Python test in PR 2
├── cmd/authlimit/main.go                # serve | healthcheck | keys {init,rotate,list} | revoke
├── cmd/echo/main.go                     # stub upstream for the smoke: returns the headers it received
└── internal/
    ├── config/                          # environment parsing, fail-closed startup checks
    ├── extjwt/                          # external token verifier + JWKS cache with stale grace
    ├── identity/                        # keyset, internal-identity minting, public JWKS
    ├── limiter/                         # route classes, bucket script, observe/enforce
    ├── revoke/                          # revocation list: write, poll, check
    ├── server/                          # /v1/check, /healthz, /readyz, /internal/jwks.json
    ├── errenv/                          # {code,message,details} envelope
    └── telemetry/                       # OTLP metrics, slog

testdata/auth-vectors/                   # PR 1: shared vectors for Go and Python
├── README.md  jwks.json  vectors.json   # pre-signed tokens, an injected clock, expected outcomes (no private key kept)

scripts/gen_auth_vectors.py              # PR 1: how the vectors were made (throwaway key, discarded)
scripts/mcp_gateway_bootstrap.py         # PR 1: + API application (resource, scope, redirect URI); token default 168 h, --token-hours
scripts/authlimit_smoke.py               # PR 1 (stub upstream), extended in PR 2 (real API)
deploy/caddy/Caddyfile.gateway           # PR 1: + api site (forward_auth chat routes; limit-only MCP routes)
deploy/caddy/Caddyfile                   # PR 2: production site wired for identity mode (cuttable)
deploy/compose/docker-compose.yml        # PR 1: authlimit x2, authlimit-echo, key directory; PR 2: api behind it
deploy/compose/docker-compose.prod.yml   # PR 2: authlimit x2, required issuer/keys variables (cuttable)
observability/prometheus/alerts.yml      # PR 1: service alerts; PR 2: ApiHeaderAuthMode
tests/core/test_alert_rules.py           # PR 1: also reads metric names from the Go service
tests/core/test_authlimit_*.py           # PR 1: compose, Caddyfile, boundary (the service imports nothing from app/)
.github/workflows/ci.yml                 # PR 1: `authlimit` job (vet, test -race, image build)
Makefile                                 # PR 1: authlimit-test/-build/-keys/-up/-smoke/-down/-revoke

app/core/internal_identity.py            # PR 2: verifier + public-key cache (imports nothing from agent/domains/api)
app/api/deps.py                          # PR 2: get_ctx by mode
app/api/rate_limit.py / main.py          # PR 2: tenant limiter only in header mode; webhook limit unchanged
app/api/static/                          # PR 2: sign-in (authorization code + PKCE) replaces the tenant selector in identity mode
app/core/config.py / .env.example        # PR 2: AUTH_MODE and the key location
app/core/errors.py                       # PR 2: new ErrorCode values, checked against errorcodes.json
tests/api/ tests/core/                   # PR 2: identity-mode tests, route-table drift, shared vectors for mcp-gateway
```

**Structure Decision**: a new top-level `services/` directory for deployable non-Python services, one directory each, self-contained like `packages/billing/` (own module, own tests, no import of repository code; the only couplings are the contract, the shared vectors and two JSON registries). The existing files are modified in place (the repository is not running in production, A1).

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| A service in Go (a second language and toolchain) | The maintainer's decision (spec Decisions 1); it keeps verification and limiting out of the API process and gives one policy point for the API and MCP routes. | The Python shared module would meet the requirements with less to run, and the same code could have been served as `forward_auth`. Rejected by the maintainer's choice; the cost is a second toolchain, CI job, image and the constitution amendment. |
| Two PRs over the size ceiling | The maintainer's instruction (exactly two PRs). | More, smaller PRs would match the repository's guidance. The split points are named in the spec's Delivery section. |
| Verifying the external token in two places (the service, and `mcp-gateway`) | The MCP authorization specification requires the MCP server to validate tokens itself. | Trusting the front door alone for MCP is a grey area under the specification and would rewrite merged spec 017 code. Shared vectors keep the two in step (FR-034). |
