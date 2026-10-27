# Research: authlimit (spec 018)

Decisions, what was measured and what was not. Written 2026-10-27. Anything marked **unverified** is a task to settle, not a fact.

## R1. How Caddy's `forward_auth` really behaves (measured)

**Setup.** `caddy:2.11.7` (the repository's pinned image, digest `sha256:504a44d7…`), one container, in front of two throwaway loopback fakes: an auth service that decides by the `Authorization` header and records what it received, and an upstream that records what it received. Source read as well: `modules/caddyhttp/reverseproxy/forwardauth/caddyfile.go` (it expands to a `reverse_proxy` with method GET, a rewritten URI, `X-Forwarded-Method`/`X-Forwarded-Uri`, and a 2xx response handler that, for each `copy_headers` name, first *deletes* the header from the request and then sets it from the auth response). The fakes and script were scratchpad files, not committed; the table is the record.

| Question | Result |
|---|---|
| Does the auth service receive `Authorization`? | **Yes**, unchanged. It also receives `X-Forwarded-Method`, `X-Forwarded-Uri` (path and query), `X-Forwarded-For` (the client address), `-Host` and `-Proto`. |
| What does it receive as a request? | A **bodiless GET** to the configured `uri`, whatever the original method. A 100 KB POST reached the upstream whole (102400 bytes) and the auth service as a GET with no body. |
| Can a client-sent identity header survive? | **No.** Forged value + auth returns the real one: upstream sees only the real one. Forged value + auth returns 200 *without* the header: upstream sees none. Duplicate headers and lower-case spelling: none. |
| Does the upstream still get the caller's `Authorization`? | **Yes**, by default. `request_header -Authorization` placed after `forward_auth` removes it (checked). |
| 401 from the auth service | Reaches the client with status, body and `WWW-Authenticate`; upstream not called. |
| 429 from the auth service | Reaches the client with `Retry-After`; upstream not called. |
| `lb_try_duration` / `lb_try_interval` inside `forward_auth` | Work. Two auth upstreams, the first dead: **20 of 20** requests succeeded with a 3 s retry window; **7 and 9 of 20** (13 and 11 failed) without it, in two runs. |
| `dynamic a { name … port … refresh … }` inside `forward_auth` | Works. |
| Every auth upstream unreachable | **502** in about 14 ms, upstream not called. Fail closed. |

**Decision.** `forward_auth` with `uri /v1/check`, `copy_headers X-Internal-Identity`, `dynamic a` over the service name, `lb_try_duration 5s` and `lb_try_interval 250ms` (the values spec 017's Caddyfile uses), and `request_header -Authorization` after it on the chat routes. The identity header name is `X-Internal-Identity`.

**Not measured:** streaming behaviour of an answer that starts after the check (the check is a separate subrequest, so none is expected), TLS between Caddy and the service (plain http inside the compose network), and behaviour under load.

## R2. What Casdoor puts in a token (partly unverified)

Known from spec 017 (`research.md` M2): format `JWT-Standard` (17 claims, 16 in the live token); `owner` is the organization (the tenant), `sub` a stable user id; `aud` follows the `resource` parameter; Casdoor does **not** validate the `resource`; public client with PKCE works; discovery and an introspection endpoint exist; the issuer follows the Host header unless `origin` is set.

**Unverified: whether a `JWT-Standard` token has `iat`** (the claim count is known, the list was not recorded; the repository's own test fixture sets `iat`, which proves nothing about Casdoor). FR-002 and revocation both need an issue time. Task T-IAT captures a real token from the running stack and writes its claim list here. If `iat` is absent: first try a Casdoor token format that includes it (`JWT-Custom` lets the application choose fields; measure); only if that fails, estimate it as `exp − configured lifetime`, which is wrong if the lifetime was changed after issue and would be recorded as a known gap.

**Unverified: the application type for the API.** Spec 017 found that category `Agent` / type `MCP` turns on custom `resource:action` scopes and that `openid` is an invalid scope there. The API needs a custom scope (`api:chat`) and a public client with a browser redirect; T-APP creates the application as the same kind and checks the web flow works with it.

## R3. Token lifetimes

`scripts/mcp_gateway_bootstrap.py` sets `TOKEN_HOURS = 1` and `REFRESH_HOURS = 168` for the MCP applications (spec 017). The maintainer's default is 7 days. **Decision:** one `--token-hours` option (and an environment variable), default **168**, for every application the bootstrap creates; refresh stays 168 (a refresh token adds nothing at an equal lifetime, and is how an operator would later shorten the access token without logging people out). The service's cap (`AUTHLIMIT_MAX_TOKEN_LIFETIME`, default 168 h) compares `exp − iat`, so Casdoor's exact 168 h passes and a longer one is refused. Recommended production setting, documented: a 15 to 60 minute access token with refresh. The risk of the long default is that a stolen token is good for a week; revocation (R8) is the bound.

## R4. Go toolchain and libraries

- **Go**: build stage `golang:1.26@sha256:37cfcca6…` (cached locally; the digest is re-read at build time); the host has Go 1.27.1, which is only used for editing and `go test`.
- **JWT**: `github.com/golang-jwt/jwt/v5` v5.3.1, with an explicit `WithValidMethods`, `WithAudience`, `WithIssuer`, `WithLeeway` and `WithExpirationRequired`. Chosen over `lestrrat-go/jwx` (a larger surface with its own JWKS cache whose stale-key behaviour would have to be bent to the grace rule) and `MicahParks/keyfunc` (same). The external token allow-list is **RS256 only** (what Casdoor signs with); adding ES256 later is one entry.
- **JWKS**: a small purpose-built cache (fetch with a 5 s timeout, parse RSA keys with `crypto/rsa`, TTL, refresh on an unknown `kid` at most once per 30 s, stale grace). It mirrors spec 017's `JwksCache` rules; the shared vectors keep the two honest.
- **Redis**: `go-redis/v9`. **Metrics**: OpenTelemetry Go SDK, OTLP/HTTP to `host.docker.internal:4318`, the endpoint `mcp-gateway` uses. **Logs**: `log/slog` JSON. **HTTP**: `net/http` with the Go 1.22 pattern mux.
- **Tests**: `miniredis/v2` for the limiter. **Unverified:** that it runs the bucket script (it embeds a Lua interpreter, but `TIME` and some `redis.call` forms may differ); fallback is passing `now` in from Go (clock skew between replicas is then bounded by NTP, which is acceptable for one host) and running the script against the real Redis in the smoke.

## R5. The internal identity is a short-lived EdDSA JWT

**Decision.** A JWT signed with Ed25519 (`alg: EdDSA`), header `kid` and `typ: authlimit-identity+jwt`, claims `iss: urn:agent-core:authlimit`, `aud: agent-core-api`, `sub` (principal), `tenant`, `scope`, `jti`, `iat`, `nbf`, `exp` (30 s default, 120 s maximum). The API verifies it with the public key only.

**Checked here (PyJWT 2.14, `cryptography`):** a token signed with Ed25519 verifies with the public key; a wrong audience (`InvalidAudienceError`), wrong issuer (`InvalidIssuerError`), a wrong allowed-algorithm list (`InvalidAlgorithmError`) and a different Ed25519 key (`InvalidSignatureError`) are all refused. **Unverified:** `golang-jwt` signing with `ed25519.PrivateKey` and the Go-signed/Python-verified round trip: the first PR 1 test.

**Alternatives.** HMAC with a shared secret: rejected, any consumer could then mint identities. PASETO: no library already in either stack. An opaque token plus an introspection call: a network call per request. Passing plain headers (`X-Tenant-Id`) from the gateway: relies on the network path and on header stripping, and the constitution makes tenant isolation non-negotiable. mTLS between Caddy and the service: named, not built.

**Confusion defences.** The internal identity has a different issuer, audience, `typ`, algorithm and key set from the external token, and each verifier pins all of them, so neither can be accepted as the other (shared vectors include both directions).

## R6. Latency

PyJWT verifies one 17-claim RS256 token in **55 µs** on one core (5,000 iterations of `jwt.decode` with PyJWT 2.14, single thread, no I/O, a throwaway script that is not committed). Go is expected to be at least as fast and was not measured. The added time per request will be dominated by the extra internal hop and one Redis round trip; SC-008 is measured in the smoke on the local stack and the result is written here.

## R7. The token bucket

**Decision.** One atomic Redis script per request, both buckets in one call: it reads Redis `TIME`, refills each bucket (`tokens = min(capacity, tokens + elapsed × rate)`), and consumes one from each only if *both* have at least one; otherwise it consumes nothing and returns the wait until the emptier one has a token (`Retry-After`, rounded up to whole seconds, minimum 1). State is two integers per bucket (millitokens, last-refill milliseconds) so no floating point drifts; the key expires after twice the time to fill from empty.

Keys: `authlimit:v1:b:{<tenant>}:t:<class>` and `authlimit:v1:b:{<tenant>}:p:<principal>:<class>`. The braces are a hash tag, so both keys of one request share a slot if the store is ever clustered.

Defaults (A3): tenant bucket refill 30 per minute (0.5 per second), capacity 10; person bucket refill 10 per minute, capacity 5; per route class, overridable. Modes: `enforce` and `observe` (`AUTHLIMIT_LIMIT_MODE`), where `observe` counts and logs the would-be 429 and serves. The call has a 100 ms ceiling; on error or timeout the request is served and `store_errors_total{store="limiter"}` moves.

**Alternatives.** `limits` (the app's library) has fixed-window, moving-window and sliding-window-counter strategies only (checked on 5.8.0); no bucket. GCRA is equivalent; a bucket was chosen because it reads plainly and matches the published design the maintainer asked for. A Caddy rate-limit module: a custom Caddy build and a sliding window.

## R8. Revocation

**Decision.** "Tokens issued to this person before moment T are void." Stored as one sorted set `authlimit:v1:revoked`, member `<tenant>\x00<sub>`, score `T` (Unix seconds). `authlimit revoke --tenant T --sub S` adds it (and trims entries older than the lifetime cap). Each replica polls the whole set every second into memory and checks a request with a map lookup (no Redis call per request). A token is void when `iat ≤ T`: sign-in in the same second as the revocation is also void, so "sign in again" works from the next second (documented). FR-019's 5 seconds is the poll interval plus a margin.

If the poll fails the replica keeps enforcing its last copy; once the copy is older than `AUTHLIMIT_REVOCATION_MAX_STALENESS` (30 s) it refuses requests (`closed`, default) or serves (`open`). This also means a Redis blip never stalls a request and the limiter's fail-open policy and the revocation fail-closed policy do not collide (an earlier idea of one combined script call would have forced a single policy for both). The set is expected to stay small (people revoked in the last week); a bound of 10,000 entries is documented and exceeding it is logged.

Revocation applies to the MCP routes too (the service runs on them in limit-only mode), which fills a gap spec 017 left open.

## R9. Stale-key grace

Keys are cached for 300 s (`AUTHLIMIT_JWKS_CACHE_SECONDS`), refreshed in the background and on an unknown `kid` at most once per 30 s. If a refresh fails, the cached keys keep being used for up to **6 hours** (`AUTHLIMIT_JWKS_STALE_GRACE_SECONDS`); the first failure moves `key_refresh_total{result="error"}` and, after 5 minutes, raises `AuthlimitKeysStale`. Beyond the grace the replica reports not ready and refuses everything with a counted reason. Why six hours: long enough to ride out an outage and its repair on one host, short enough that a key the authorization server has withdrawn for compromise is not trusted for days. Casdoor's own key rotation is picked up by the unknown-`kid` refresh, so the grace does not delay it unless Casdoor is unreachable.

## R10. Internal key custody and rotation

A directory (`AUTHLIMIT_KEYS_DIR`, read-only in the container) holding `keyset.json` and one PKCS#8 PEM per key id. The manifest lists, per key: `kid`, `publish_at`, `use_from`, `retire_at`. A replica signs with the newest key whose `use_from` has passed, publishes (at `/internal/jwks.json`) every key whose `publish_at` has passed and `retire_at` has not, and refuses to start with no usable signing key. `authlimit keys rotate` creates a key and adds an entry with `publish_at = now`, `use_from = now + overlap` and sets the old key's `retire_at = use_from + overlap`; the overlap defaults to 10 minutes (consumers cache the public set for 60 s and the identity lives 30 s, so 10 minutes has wide margin; the smoke rotates with a short overlap). `keys init` creates the first key for the development stack in a directory outside the repository, with the convention `mcp-gateway-secrets` uses (mode 700, never overwritten). Replicas share the directory by mounting it; a managed secret store is the production step (not built).

The API fetches `/internal/jwks.json` from the service over the compose network (not routed by Caddy), caches it for 60 s and refreshes on an unknown `kid` at most once per 10 s. **Trust assumption, stated in the threat model:** whoever can answer that request inside the network can make the API trust a forged identity; the network is the boundary until mTLS or pinning (named, not built).

## R11. Route classes

`routes.json` is the table (embedded default, file override). Classes: `turn` (verified, tenant and person buckets), `upload` (same, its own parameters), `read` and `cancel` (verified, never limited), `public` (not checked: health, readiness, the page and its assets, the payment webhook), `mcp` (limit-only), and a fallback for any unlisted route (verified and limited like a turn). The defaults mirror today's API: limited `POST /chat/stream/queued`, `POST /chat/resume`, `POST /ingest/upload`; unlimited but verified `POST /chat/cancel`, `GET /chat/sessions`, `GET /chat/sessions/{thread_id}/messages`, `GET /chat/sessions/{thread_id}/pending_approval`, `GET /usage`, `GET /ingest/stream/{job_id}`; public `GET /`, `/health`, `/health/ready`, static assets and `/billing/webhooks/*`. In Caddy, public paths never call the service at all; the service also classifies them as public, so a mistake in one place is caught by the other. PR 2 adds a test that walks the API's routes and fails on any not in the table. (The billing router's own route list is read by that test from the app.)

## R12. MCP limit-only mode

On `mcp` routes the service parses the bearer token, verifies signature, issuer and expiry only (not audience or scope, which `mcp-gateway` decides per endpoint), and takes tenant and person from it. Valid and not revoked: it applies the buckets and answers 200 without an identity header. Missing or invalid token: it answers **200 and does not limit**, so the MCP gateway produces the canonical 401 with its discovery metadata (an MCP client depends on that exact answer). Revoked: it answers 401 with `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<url>"`, the URL built from `AUTHLIMIT_MCP_METADATA_URL_TEMPLATE`. The `/.well-known/oauth-protected-resource/*` paths are public. Consequence, disclosed: until the Cloudflare step, an unauthenticated flood on the MCP routes reaches `mcp-gateway`.

## R13. Error envelope

The service answers refusals as `{"code": …, "message": …, "details": {…}}`, the repository's envelope. Codes: `unauthenticated` (401), `forbidden` (403, wrong scope, with `details.required_scope`), `rate_limited` (429, with `details.retry_after_seconds`), `auth_unavailable` (503, keys or revocation list unusable), `internal` (500). The list lives in `services/authlimit/errorcodes.json`; PR 2 adds the values to `ErrorCode` and a test that fails if the Python registry and the file disagree. Messages are written for the caller and never contain a token, a claim or an exception's text.

## R14. Observability

Metrics (OTLP counters, histograms and gauges, names as they appear in `alerts.yml`):

- `agent_authlimit_requests_total{route_class, outcome, reason}`: outcome `allowed`, `refused`, `limited`, `would_limit`; reason one of `missing_token`, `malformed`, `bad_signature`, `bad_issuer`, `bad_audience`, `expired`, `no_issue_time`, `lifetime_too_long`, `bad_scope`, `revoked`, `keys_unavailable`, `revocation_unavailable`, `unlisted_route`.
- `agent_authlimit_request_duration_seconds` (histogram, explicit buckets), `agent_authlimit_key_refresh_total{result}`, `agent_authlimit_keys_age_seconds`, `agent_authlimit_revocation_age_seconds`, `agent_authlimit_store_errors_total{store}`, `agent_authlimit_ready` (gauge, 1/0, one series per replica), `agent_authlimit_signing_key_age_seconds`.
- Labels never carry a tenant, principal or token.

Alerts: `AuthlimitCannotCheckTokens`, `AuthlimitKeysStale`, `AuthlimitLimiterStoreDown`, `AuthlimitRevocationStale`, `AuthlimitReplicasDown`, `AuthlimitLimiterObserveOnly` (info: still in observation mode after a day), and, in PR 2, `ApiHeaderAuthMode`. `tests/core/test_alert_rules.py` today only knows metrics defined in Python; it is extended to read the Go service's names from a single source file, so a typo in an alert fails the build.

## R15. Deployment

`docker-compose.yml`, profile `gateway`: `authlimit` with `deploy.replicas: 2` (checked: `docker compose up --dry-run` creates two containers from `deploy.replicas` on Compose 5.1.3), hardened like `mcp-gateway` (read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, non-root), a `healthcheck` that runs `authlimit healthcheck` (the image has no shell or curl), `depends_on` Redis and Casdoor healthy, the key directory mounted read-only, and `authlimit-echo` (the stub upstream, PR 1). `Caddyfile.gateway` gains an `api` site on the same port and local CA. The image: build stage by digest, `gcr.io/distroless/static` non-root as the runtime (its digest is read and pinned when the Dockerfile is written). CI: an `authlimit` job (go vet, `go test -race`, image build) next to `billing-package`; the existing trivy and checkov jobs see the new files.

## R16. Web UI sign-in (PR 2)

The UI streams chat with `fetch()` and reads the response body (the file says `EventSource` was avoided because it cannot set headers), so adding `Authorization: Bearer …` to each call is a small change. Authorization code with PKCE against Casdoor as a public client, redirect to the front-door origin, the token kept in `sessionStorage`. The residual risk, stated in the threat model: script injection on the page can read a token in storage; a server-side session cookie would avoid that and is a larger change (not built). In identity mode the tenant selector is not shown and its value is never sent. Expiry: on a 401 the page signs in again (refresh-token use is the cuttable extra).

## R17. The existing in-app limiter

`app/api/rate_limit.py` keys on `X-Tenant-Id` (evadable) and counts the same requests the service now counts. **Decision (maintainer, 2026-10-27, FR-029): remove it**, in both modes; an earlier draft of this plan gated it by mode, which would have kept dead code and a second counter alive for the sake of a development path that needs no rate limit. Only the payment-webhook per-address limit stays (this service does not cover it). Spec 004's FR-027, the README "Limits" line, pattern 43, the settings entry `RATE_LIMIT_PER_MINUTE`, its alert and `tests/api/test_rate_limit.py` change with it (task T063, PR 2).

## R18. Shared token vectors

A Python script signs a set of tokens once with a throwaway RSA key, keeps only the public key as `jwks.json` and the tokens with their expected outcomes in `vectors.json` (the clock is part of each vector, so no vector expires), and discards the private key. No private key is ever committed, which also keeps the secret scanner quiet about key material. **Unverified:** whether Trivy's secret rules flag the JWT strings; T-VEC runs the scan on the directory and adds a narrow allow rule with a reason if so. The set covers at least: valid; expired; not yet valid; missing `exp`, `sub`, `iat`; wrong issuer; wrong audience; client id as audience; `alg: none`; HS256 signed with the public key; unknown `kid`; signature from a foreign key; lifetime over the cap; missing scope; tenant or principal not text; an internal identity presented as an external token and the reverse.

## R19. Alternatives considered for the whole design

| Alternative | Why not |
|---|---|
| A shared Python module in `api` and `mcp-gateway` | Smaller and the better start at this size; the maintainer chose the service. |
| Envoy (`jwt_authn`, local token bucket) | Heavier to operate; its local limiter is per process and a shared one needs another service. |
| Traefik | The open-source edition has no JWT validation (Hub only); its limiter is a bucket, with optional Redis. |
| Caddy plugins (JWT, `caddy-ratelimit`) | A custom Caddy build; sliding window; approximate in a cluster. |
| `oauth2-proxy` | Validates bearer JWTs and audience but has no documented way to map the `owner` claim to a header and has no rate limiting. |
| Cloudflare for per-tenant limits | Per-tenant keys need the Enterprise plan; it is the later per-address layer. |

## R20. Open items this plan carries into tasks

`iat` in Casdoor tokens (R2); the API application type (R2); `miniredis` and the bucket script (R4); Go-signed, Python-verified EdDSA (R5); Trivy and the vectors (R18); the measured added latency (R6); that two replicas come up healthy under `docker compose up` with the hardening settings (R15).
