# Research: authlimit (spec 018)

Decisions, what was measured and what was not. Written 2026-10-27; updated after PR 1 was built (R21 has the measured results of the real-stack run). Anything still marked **unverified** is a task to settle, not a fact.

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

## R2. What Casdoor puts in a token (measured)

Known from spec 017 (`research.md` M2): format `JWT-Standard` (17 claims, 16 in the live token); `owner` is the organization (the tenant), `sub` a stable user id; `aud` follows the `resource` parameter; Casdoor does **not** validate the `resource`; public client with PKCE works; discovery and an introspection endpoint exist; the issuer follows the Host header unless `origin` is set.

**Measured (T006, 2026-10-27, Casdoor 4.24.0 behind Caddy, an `api-ecorp` application made by the bootstrap, a throwaway user, public client with PKCE, `resource=https://api.localhost:8443`, `scope=api:chat`):**

- Header: `{"alg": "RS256", "kid": "cert-built-in", "typ": "JWT"}`.
- Claims of a `JWT-Standard` access token: `aud` (a list), `auth_time`, `azp`, `email`, `exp`, **`iat`**, `id`, `iss`, **`jti`**, `name`, `nbf`, `owner`, `preferred_username`, `scope` (the string `api:chat`), `sub`, `tokenType` (`access-token`). So **`iat` is present** (FR-002 and revocation can rely on it) and so is `jti` (revocation by token id remains possible later; the per-person cut-off needs neither).
- `exp - iat` = **604800 s, exactly 168 h**, equal to the default cap, which is why the cap compares with `>` (a token at the cap is valid) and why the vectors include "exactly the lifetime cap". `expires_in` in the token response agrees; a refresh token is issued.
- `iss` is `https://auth.localhost:8443` (the `origin` setting is pinned in the compose file).
- `aud` follows `resource`: with `resource` it is `["https://api.localhost:8443"]`; **without it, `["<client id>"]`** (spec 017's finding, reproduced: the "client id as audience" vector is a real case).
- `scope=openid` is refused at login ("Invalid scope") on the `Agent`/`MCP` application type, as spec 017 found; `scope=api:chat` works. **The application type needed for a custom scope also works for a browser public client with a redirect to the page's root URL and to the smoke check's callback**, so the API application is the same kind as the MCP ones (the answer to the second open question above).
- The claims the verifier reads are the ones it expects: `owner` (the organization: the tenant) and `sub`.

## R3. Token lifetimes

`scripts/mcp_gateway_bootstrap.py` sets `TOKEN_HOURS = 1` and `REFRESH_HOURS = 168` for the MCP applications (spec 017). The maintainer's default is 7 days. **Decision:** one `--token-hours` option (and an environment variable), default **168**, for every application the bootstrap creates; refresh stays 168 (a refresh token adds nothing at an equal lifetime, and is how an operator would later shorten the access token without logging people out). The service's cap (`AUTHLIMIT_MAX_TOKEN_LIFETIME`, default 168 h) compares `exp − iat`, so Casdoor's exact 168 h passes and a longer one is refused. Recommended production setting, documented: a 15 to 60 minute access token with refresh. The risk of the long default is that a stolen token is good for a week; revocation (R8) is the bound.

## R4. Go toolchain and libraries

- **Go**: build stage `golang:1.26@sha256:37cfcca6…` (cached locally; the digest is re-read at build time); the host has Go 1.27.1, which is only used for editing and `go test`.
- **JWT**: `github.com/golang-jwt/jwt/v5` v5.3.1, with an explicit `WithValidMethods`, `WithAudience`, `WithIssuer`, `WithLeeway` and `WithExpirationRequired`. Chosen over `lestrrat-go/jwx` (a larger surface with its own JWKS cache whose stale-key behaviour would have to be bent to the grace rule) and `MicahParks/keyfunc` (same). The external token allow-list is **RS256 only** (what Casdoor signs with); adding ES256 later is one entry.
- **JWKS**: a small purpose-built cache (fetch with a 5 s timeout, parse RSA keys with `crypto/rsa`, TTL, refresh on an unknown `kid` at most once per 30 s, stale grace). It mirrors spec 017's `JwksCache` rules; the shared vectors keep the two honest.
- **Redis**: `go-redis/v9`. **Metrics**: OpenTelemetry Go SDK, OTLP/HTTP to `host.docker.internal:4318`, the endpoint `mcp-gateway` uses. **Logs**: `log/slog` JSON. **HTTP**: `net/http` with the Go 1.22 pattern mux.
- **Tests**: `miniredis/v2` for the limiter. **Measured:** it runs the bucket script including `redis.call('TIME')` (a test sets its clock with `SetTime`), so the script reads Redis's own clock in production and the hermetic tests need no override; the override argument stays for tests that want a hand-moved clock. Versions resolved: `golang-jwt/jwt/v5` 5.3.1, `go-redis/v9` 9.23.0, OpenTelemetry Go 1.47.0, `miniredis/v2` 2.40.0, built with `golang:1.26`.

## R5. The internal identity is a short-lived EdDSA JWT

**Decision.** A JWT signed with Ed25519 (`alg: EdDSA`), header `kid` and `typ: authlimit-identity+jwt`, claims `iss: urn:agent-core:authlimit`, `aud: agent-core-api`, `sub` (principal), `tenant`, `scope`, `jti`, `iat`, `nbf`, `exp` (30 s default, 120 s maximum). The API verifies it with the public key only.

**Checked here (PyJWT 2.14, `cryptography`):** a token signed with Ed25519 verifies with the public key; a wrong audience (`InvalidAudienceError`), wrong issuer (`InvalidIssuerError`), a wrong allowed-algorithm list (`InvalidAlgorithmError`) and a different Ed25519 key (`InvalidSignatureError`) are all refused. **Measured (interop):** an identity minted by the Go service (`golang-jwt`, `ed25519.PrivateKey`) verifies in PyJWT 2.14 from the OKP JWK the service serves (`jwt.PyJWK(jwk).key`), and wrong audience, wrong issuer, a wrong algorithm list and a foreign key are each refused. A golden file (`testdata/auth-vectors/identity.json`: a Go-minted token and the public key set, no private key) lets PR 2's Python verifier keep checking it. One harness note: PyJWT reads the clock with `datetime.now`, not `time.time`, so a test that fixes the clock must pass options or patch that, not `time.time`.

**Alternatives.** HMAC with a shared secret: rejected, any consumer could then mint identities. PASETO: no library already in either stack. An opaque token plus an introspection call: a network call per request. Passing plain headers (`X-Tenant-Id`) from the gateway: relies on the network path and on header stripping, and the constitution makes tenant isolation non-negotiable. mTLS between Caddy and the service: named, not built.

**Confusion defences.** The internal identity has a different issuer, audience, `typ`, algorithm and key set from the external token, and each verifier pins all of them, so neither can be accepted as the other (shared vectors include both directions).

## R6. Latency

PyJWT verifies one 17-claim RS256 token in **55 µs** on one core (5,000 iterations of `jwt.decode`, single thread, no I/O, a throwaway script that is not committed), so verification was never the concern. **Measured on the real stack (`make authlimit-latency`, 500 alternating pairs after a 50-pair warm-up, one TLS connection, Caddy and the stub upstream in Docker on a laptop):**

| Route | Checked route | Public route (no check) | Added by the check |
|---|---|---|---|
| `GET /chat/sessions` (verify, revocation lookup, mint) | median 1.12 ms, p95 2.14 ms | median 0.77 ms, p95 1.30 ms | **+0.36 ms median, +0.84 ms p95** |
| `POST /chat/stream/queued` in observe mode (adds the Redis bucket call) | median 1.49 ms, p95 2.62 ms | median 1.02 ms, p95 2.06 ms | **+0.46 ms median, +0.57 ms p95** |

Against the proposed targets (median under 10 ms, p95 under 50 ms) both are met by more than an order of magnitude. This is a development stack on one machine, through Docker Desktop's network: a floor for the cost of the design, not a production figure, and it says nothing about behaviour under load (not measured).

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

Keys are cached for 300 s (`AUTHLIMIT_JWKS_CACHE_SECONDS`), refreshed in the background and on an unknown `kid` at most once per 30 s. If a refresh fails, the cached keys keep being used for up to **6 hours past the TTL** (`AUTHLIMIT_JWKS_STALE_GRACE_SECONDS`: a key set is usable while its age is at most the TTL plus the grace; fresh up to the TTL, stale after it); the first failure moves `key_refresh_total{result="error"}` and, after 5 minutes, raises `AuthlimitKeysStale`. Beyond the grace the replica reports not ready and refuses everything with a counted reason. Why six hours: long enough to ride out an outage and its repair on one host, short enough that a key the authorization server has withdrawn for compromise is not trusted for days. Casdoor's own key rotation is picked up by the unknown-`kid` refresh, so the grace does not delay it unless Casdoor is unreachable.

## R10. Internal key custody and rotation

A directory (`AUTHLIMIT_KEYS_DIR`, read-only in the container) holding `keyset.json` and one PKCS#8 PEM per key id. The manifest lists, per key: `kid`, `publish_at`, `use_from`, `retire_at`. A replica signs with the newest key whose `use_from` has passed, publishes (at `/internal/jwks.json`) every key whose `publish_at` has passed and `retire_at` has not, and refuses to start with no usable signing key. `authlimit keys rotate` creates a key and adds an entry with `publish_at = now`, `use_from = now + overlap` and sets the old key's `retire_at = use_from + overlap`; the overlap defaults to 10 minutes (consumers cache the public set for 60 s and the identity lives 30 s, so 10 minutes has wide margin; the smoke rotates with a short overlap). `keys init` creates the first key for the development stack in a directory outside the repository, with the convention `mcp-gateway-secrets` uses (mode 700, never overwritten). Replicas share the directory by mounting it; a managed secret store is the production step (not built).

The API fetches `/internal/jwks.json` from the service over the compose network (not routed by Caddy), caches it for 60 s and refreshes on an unknown `kid` at most once per 10 s. **Trust assumption, stated in the threat model:** whoever can answer that request inside the network can make the API trust a forged identity; the network is the boundary until mTLS or pinning (named, not built).

## R11. Route classes

`routes.json` is the table (embedded default, file override). Classes: `turn` (verified, tenant and person buckets), `upload` (same, its own parameters), `read` and `cancel` (verified, never limited), `public` (not checked: health, readiness, the page and its assets, the payment webhook), `mcp` (limit-only), and a fallback for any unlisted route (verified and limited like a turn). The defaults mirror today's API: limited `POST /chat/stream/queued`, `POST /chat/resume`, `POST /ingest/upload`; unlimited but verified `POST /chat/cancel`, `GET /chat/sessions`, `GET /chat/sessions/{thread_id}/messages`, `GET /chat/sessions/{thread_id}/pending_approval`, `GET /usage`, `GET /ingest/stream/{job_id}`; public `GET /`, `/health`, `/health/ready`, static assets and `/billing/webhooks/*`. In Caddy, public paths never call the service at all; the service also classifies them as public, so a mistake in one place is caught by the other. PR 2 adds a test that walks the API's routes and fails on any not in the table. (The billing router's own route list is read by that test from the app.)

## R12. MCP limit-only mode

On `mcp` routes the service parses the bearer token, verifies signature, issuer and expiry only (not audience or scope, which `mcp-gateway` decides per endpoint), and takes tenant and person from it. Valid and not revoked: it applies the buckets and answers 200 without an identity header. Missing or invalid token: it answers **200 and does not limit**, so the MCP gateway produces the canonical 401 with its discovery metadata (an MCP client depends on that exact answer). Revoked: it answers 401 with `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<url>"`, the URL built from `AUTHLIMIT_MCP_METADATA_URL_TEMPLATE`. The `/.well-known/oauth-protected-resource/*` paths are public. Consequence, disclosed: until the Cloudflare step, an unauthenticated flood on the MCP routes reaches `mcp-gateway`. The MCP buckets are larger than the chat turns' (tenant 60 tokens refilling 120 a minute, person 30 and 60) because an MCP tool is a cheap read and one client session makes several requests (the 017 smoke needs them); the chat turn's 30 a minute per tenant is sized for an LLM call.

## R13. Error envelope

The service answers refusals as `{"code": …, "message": …, "details": {…}}`, the repository's envelope. Codes: `unauthenticated` (401), `forbidden` (403, wrong scope, with `details.required_scope`), `rate_limited` (429, with `details.retry_after_seconds`), `auth_unavailable` (503, keys or revocation list unusable), `internal` (500). The list lives in `services/authlimit/errorcodes.json`; PR 2 adds the values to `ErrorCode` and a test that fails if the Python registry and the file disagree. Messages are written for the caller and never contain a token, a claim or an exception's text.

## R14. Observability

Metrics (OTLP counters, histograms and gauges, names as they appear in `alerts.yml`):

- `agent_authlimit_requests_total{route_class, outcome, reason}`: outcome `allowed`, `passthrough`, `refused`, `limited`, `would_limit`; reason one of `missing_token`, `malformed`, `bad_algorithm`, `bad_signature`, `bad_issuer`, `bad_audience`, `expired`, `not_yet_valid`, `no_issue_time`, `lifetime_too_long`, `bad_claims`, `bad_scope`, `revoked`, `keys_unavailable`, `revocation_unavailable`, `signing_unavailable`, `unlisted_route` (`internal/reason`; the same words are log fields and test expectations).
- `agent_authlimit_request_duration_seconds` (histogram, explicit buckets), `agent_authlimit_key_refresh_total{result}`, `agent_authlimit_keys_age_seconds`, `agent_authlimit_revocation_age_seconds`, `agent_authlimit_store_errors_total{store}`, `agent_authlimit_ready` (gauge, 1/0, one series per replica), `agent_authlimit_signing_key_age_seconds`.
- Labels never carry a tenant, principal or token.

Alerts as built: `AuthlimitCannotCheckTokens`, `AuthlimitKeysStale`, `AuthlimitLimiterStoreDown`, `AuthlimitRevocationStale`, `AuthlimitReplicasDown` (PR 1), and `ApiHeaderAuthMode` (PR 2). All five were checked with `promtool check rules`. `AuthlimitReplicasDown` fires on fewer than two ready replicas, or on a service that vanished after reporting 30 minutes ago; it deliberately does not fire on `absent()` alone, which would fire for ever where the service is not deployed (production does not run it yet). `tests/core/test_alert_rules.py` reads the Go metric names from `internal/telemetry/metrics.go`.

## R15. Deployment

`docker-compose.yml`, profile `gateway`: `authlimit` with `deploy.replicas: 2` (checked: `up --dry-run` created two containers, and the real stack ran both healthy on Compose 5.1.3), hardened like `mcp-gateway` (read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, non-root), a `healthcheck` that runs `authlimit healthcheck` (the image has no shell or curl), `depends_on` Redis and Casdoor healthy, the key directory mounted read-only, and `authlimit-echo` (the stub upstream, PR 1). `Caddyfile.gateway` gains an `api` site on the same port and local CA. The image: build stage by digest, `gcr.io/distroless/static` non-root as the runtime (its digest is read and pinned when the Dockerfile is written). CI: an `authlimit` job (go vet, `go test -race`, image build) next to `billing-package`; the existing trivy and checkov jobs see the new files.

## R16. Web UI sign-in (PR 2)

The UI streams chat with `fetch()` and reads the response body (the file says `EventSource` was avoided because it cannot set headers), so adding `Authorization: Bearer …` to each call is a small change. Authorization code with PKCE against Casdoor as a public client, redirect to the front-door origin, the token kept in `sessionStorage`. The residual risk, stated in the threat model: script injection on the page can read a token in storage; a server-side session cookie would avoid that and is a larger change (not built). In identity mode the tenant selector is not shown and its value is never sent. Expiry: on a 401 the page signs in again (refresh-token use is the cuttable extra).

## R17. The existing in-app limiter

`app/api/rate_limit.py` keys on `X-Tenant-Id` (evadable) and counts the same requests the service now counts. **Decision (maintainer, 2026-10-27, FR-029): remove it**, in both modes; an earlier draft of this plan gated it by mode, which would have kept dead code and a second counter alive for the sake of a development path that needs no rate limit. Only the payment-webhook per-address limit stays (this service does not cover it). Spec 004's FR-027, the README "Limits" line, pattern 43, the settings entry `RATE_LIMIT_PER_MINUTE`, its alert and `tests/api/test_rate_limit.py` change with it (task T063, PR 2).

## R18. Shared token vectors

A Python script signs a set of tokens once with a throwaway RSA key, keeps only the public key as `jwks.json` and the tokens with their expected outcomes in `vectors.json` (the clock is part of each vector, so no vector expires), and discards the private key. No private key is ever committed, which also keeps the secret scanner quiet about key material. **Measured (T012, trivy 0.74.0, secret scanner on `testdata/auth-vectors/` only):** Trivy's `jwt-token` rule flags the vector tokens at **MEDIUM**. The CI `trivy` hard gate fails on CRITICAL only and its report step lists HIGH and CRITICAL, and `make trivy` is HIGH,CRITICAL, so nothing reports them and no allow rule was added (one would mean a custom secret config wired into CI and the Makefile for a finding no gate sees). If a gate's severity is ever lowered to MEDIUM, this directory is the first thing it will flag; the fix then is an allow rule scoped to this path, with this paragraph as its reason. The set covers at least: valid; expired; not yet valid; missing `exp`, `sub`, `iat`; wrong issuer; wrong audience; client id as audience; `alg: none`; HS256 signed with the public key; unknown `kid`; signature from a foreign key; lifetime over the cap; missing scope; tenant or principal not text; an internal identity presented as an external token and the reverse.

## R19. Alternatives considered for the whole design

| Alternative | Why not |
|---|---|
| A shared Python module in `api` and `mcp-gateway` | Smaller and the better start at this size; the maintainer chose the service. |
| Envoy (`jwt_authn`, local token bucket) | Heavier to operate; its local limiter is per process and a shared one needs another service. |
| Traefik | The open-source edition has no JWT validation (Hub only); its limiter is a bucket, with optional Redis. |
| Caddy plugins (JWT, `caddy-ratelimit`) | A custom Caddy build; sliding window; approximate in a cluster. |
| `oauth2-proxy` | Validates bearer JWTs and audience but has no documented way to map the `owner` claim to a header and has no rate limiting. |
| Cloudflare for per-tenant limits | Per-tenant keys need the Enterprise plan; it is the later per-address layer. |

## R20. Open items this plan carried into tasks: status after PR 1

| Item | Result |
|---|---|
| `iat` in Casdoor tokens | **Present** (R2), and `jti` |
| The API application type | **Works** with a browser public client and the custom scope (R2) |
| `miniredis` and the bucket script | **Runs**, including Redis `TIME` (R4) |
| Go-signed, Python-verified EdDSA | **Verified** (R5), with a golden file for PR 2 |
| Trivy and the vectors | MEDIUM only, no gate reports it (R18) |
| The added latency | **+0.4 ms median, +0.8 ms p95** (R6) |
| Two replicas healthy under `docker compose up` with the hardening settings | **Yes**, with the host user override for the key mount (R21) |

## R21. What the real-stack run found and measured (PR 1)

Run on an isolated Compose project (its own volumes, no host ports for Redis and Postgres, so the maintainer's own stack was not touched): Casdoor 4.24.0 on Postgres, Caddy 2.11.7 with its local CA, two authlimit replicas, the stub upstream, the MCP gateway, the repo's Postgres. `make authlimit-smoke ARGS=--all`: **86 checks across 11 stages, all passing in one clean run** after the fixes below (an earlier consolidated run had one failure, a smoke-isolation fault in the check itself: see finding 5).

**Found by running it (each fixed and pinned by a test; GRAPH_PATTERNS 84, ENGINEERING_PATTERNS 86):**

1. **A path Caddy called public bypassed the check.** `/static/a%5c..%5cchat/resume` matched `/static/*` and reached the upstream with no token, because a request Caddy declares public never reaches authlimit and so authlimit's stricter reading of the path could not catch it. Caddy now treats only plain URLs (no `%`, backslash, `..`, `//`) as public.
2. **A dead replica cost the dial timeout.** The first replica-kill run: 133 requests in 14 s (target rate ~280) and one 502 from a request that spent its whole 5 s retry window on the dead address. With `fail_duration 20s`, `max_fails 1` and `dial_timeout 1s`: **0 failures in 222 and 245 requests** (two runs), longest wait 1.4 s then 0.2 s; the full run later: 0 failures, 1.3 s.
3. **The door now also removes a client's own `X-Tenant-Id` and `X-Principal-Id`**, so an API later flipped to header mode behind it still cannot be told who to act as.
4. **The mutation run found a test gap** (a forged `X-Forwarded-Uri` arriving first): 22 of 26 caught on the first run, 3 did not compile (the edit), 1 survived; after the new test **26 of 26**.
5. **A smoke-isolation fault:** the tenant bucket outlives a run, so a run started within 20 s of another found it half empty. The stage waits for the refill.

**Measured (full run):**

| Property | Result | Target |
|---|---|---|
| Valid token to identity | the identity verifies against the key on disk, names the token's tenant and person, lives 30 s, header carries `kid` and `typ`; the upstream saw no `Authorization` | FR-006 to FR-008, FR-037 |
| Forged headers (eight at once, duplicated, lower-case, forged `X-Forwarded-*`) and eight path tricks | none changed the identity or reached the upstream unauthenticated | SC-002 |
| Person bucket (5) / tenant bucket (10) / colleague / other tenant / cancel and read while limited | exactly as designed; a refused request did not charge the tenant; `Retry-After` honoured | SC-003 |
| MCP | no or bad token reached the MCP gateway's own 401 with its metadata; a valid token passed; a burst of 80 gave 29 served, 51 limited, with the envelope | FR-026 |
| Revocation | first refusal at 0.3-0.5 s, every replica by 2.6-3.2 s; also on `/ecorp/mcp` with the discovery URL; a new sign-in works | SC-007 (5 s) |
| Replica killed under traffic | 0 failed requests, worst stall 1.3 s | SC-004 |
| Observe mode | 10 turns past a bucket of 5 all served, would-be refusals logged | SC-003 |
| Internal key rotation under traffic (`--overlap 20s`) | 452-455 requests, 0 failed; the new key signed from about 19.5 s | SC-005 |
| Casdoor down (5 s cache, 15 s grace) | inside the grace the existing token worked; a new sign-in failed; beyond it requests were refused with `auth_unavailable` and a `Retry-After`; service resumed 3 s after Casdoor returned | SC-006 |
| Logs | none of the 16 tokens the run obtained, and no key material, in any service's log; authlimit logged no errors | SC-009 |
| Added latency | +0.36 ms median (read), +0.46 ms (limited, with Redis) | SC-008 |

**Mutation list (`make authlimit-mutants`), 26 of 26 caught.** Each defect is one textual edit of the real source; the tests that failed are shown by the script. The list: no audience check; no issuer check; the algorithm not pinned before the key is used; no lifetime cap; a missing issue time accepted on the chat API; no scope check; a huge clock leeway allowed; an internal identity that may live a day; limit-only mode refusing a token the MCP gateway accepts; an identity minted for 24 hours; a private key readable by others accepted; keys that never go stale; key refresh not rate-limited; a refused person still charging the tenant's bucket; a dot segment not refused; an encoded slash not refused; a stale revocation copy trusted for ever; a token issued in the revocation second working; error details not allow-listed; revocation checked after the limiter; the limiter keyed on an unverified header; the limiter failing closed; read and cancel limited too; a duplicated `X-Forwarded-Uri` trusted; MCP limit-only mode refusing an invalid token itself; an unusable key set answering 401 about the token instead of 503.

**Still unmeasured:** behaviour under load (the latency numbers are one connection, sequential), a production identity provider, anything across hosts. (The web UI sign-in and the API consuming the identity were unmeasured here and are measured in R22.)

**Size (task T053).** Hand-written lines changed (added plus deleted; `go.sum` and the three generated vector files, about 850 lines, are excluded). The plan estimated 1,500 to 2,000 for all of Part A; the first, single pull request came to about 9,250. At the maintainer's request it was split into three stacked pull requests by layer. **All three are still over the repository's ~1,000 ceiling**, and each description says so in its first paragraph.

| Area | 1a verify and mint | 1b limit, revoke, serve | 1c stack and proof |
|---|---|---|---|
| Go code, image, registries | 1,947 | 1,486 | 0 |
| Go tests | 1,607 | 1,815 | 0 |
| Python scripts | 196 | 0 | 910 |
| Python tests | 81 | 93 | 472 |
| Deploy, CI, alerts, Makefile | 22 | 113 | 277 |
| Docs and specs | 136 | 44 | 326 |
| **Total** | **3,989** | **3,551** | **1,985** |

(The columns add to a few lines more than the whole, about 9,500, because `tasks.md` changes in more than one of them.)

**Why they are not smaller.** The code and its tests are almost the whole size, and half of that is tests, on purpose: this is the one component whose failure is a security failure, the constitution makes a hermetic test suite a principle, and the shared vectors, the clock-driven key tests and the table-driven refusal cases are long because each row is a case. The cuts are at package boundaries because that is the only place a pull request still compiles and passes its own tests alone; cutting inside a package would ship a half package, or code no test reaches yet.

**Where the next cut would go, if a reviewer wants smaller pieces.** 1a: the verifier and the key cache (`internal/extjwt`, about 1,200 lines with tests) against the identity keys, configuration and logging. 1b: the limiter and revocation against the HTTP server and the binary. 1c: the wiring (compose, Caddy, the Casdoor applications) against the three scripts (smoke, latency, mutation list; 864 lines). The stack tests of 1c and the smoke are the proof of the wiring, so the scripts are the last thing to split off.


## R22. What the real API behind the door found and measured (PR 2)

Run the same way as R21 (an isolated Compose project, the maintainer's own stack untouched), with the real API in identity mode and a worker added: `make authlimit-up-app`, two tenants bootstrapped (`ecorp`, `other-co`), the worker's model pointed at `loadtest/fake_llm_server.py`, which gained a `query_employees` simulator so that a REAL tool runs under the caller's tenant with no model in the way. Casdoor 4.24.0, Caddy 2.11.7, Chromium via Playwright.

**Found by running it (each fixed and pinned):**

1. **The token exchange needs `resource` too.** The page sent `resource` (RFC 8707) on the authorize request and not on the code exchange, and Casdoor answered the exchange with a 400. The smoke's Python client had always sent it in both, and the DOM-stub tests could not know. With it the exchange returns a token for the API's audience. The comment in `auth.js` says so, and the stub test pins the field.
2. **CORS is not a problem.** The page on `api.localhost` calls the token endpoint on `auth.localhost`: Casdoor answers the preflight and the request with `access-control-allow-origin` for the page's origin (measured with curl, then by the browser doing it).
3. **A tenant's client id could not be known to the page.** Casdoor generated random ids, printed by the bootstrap after the API had started. The API applications now have the id `api-<tenant>` (the bootstrap names it on creation and on a re-run; MCP clients keep their generated ids), and the page builds the id from the organization the person types. One rule in two places (`app/api/web_auth.py::CLIENT_ID_PREFIX`, the bootstrap), held together by a test.
4. **The running container serves the image's copy of the page's files.** An edit to `auth.js` did nothing until the API was rebuilt: the first run of the exchange fix looked like it had not worked. `make authlimit-up-app` builds, so it is not a trap there; it was one for me.
5. **A smoke that selects lines from a log can select none.** The first run's "the API and the worker logged something" check failed with 0 lines, because compose prefixes a line `api-1  |`, not with the container name; the JWT-in-logs check beside it had passed on an empty set. The check that the set is not empty is what showed it (ENGINEERING_PATTERNS 88).
6. **A real API does not answer like the stub.** Cancelling a thread nobody started is a 404, not a 200; the "never limited" check now asserts what it means (the door did not answer 429).
7. **A negative run**: the same `api` stage against the API restarted in header mode behind the door fails 10 checks (rows, forged headers, the direct request, the conversation, usage), and the API's startup log carries the warning that names the consequence.

**Measured:**

| Property | Result | Spec |
|---|---|---|
| Full smoke `--upstream api --all` (refusals, api, limits, mcp, revoke, logs, observe, replica, outage) | 66 of 66 | SC-002 to SC-009 |
| Two tenants asking "list the employees" through the same door | each gets its own rows from the real tool and none of the other's | SC-002 |
| Forged `X-Tenant-Id` / `X-Principal-Id`; a VALIDLY SIGNED internal identity for the other tenant (the smoke holds the key); the same identity with no token | the turn runs as the token's tenant; 401 at the door for the last | FR-040, SC-002 |
| A request that skips the door and reaches the API directly (old headers; an identity the door did not sign) | 401 | FR-010, FR-028 |
| Another tenant asking for a conversation by id | 404 | SC-002 |
| Replica killed under traffic (real API behind it) | 0 of 214 failed, worst gap 1.3 s | SC-004 |
| Revocation | first refusal 0.8 s, every replica 2.9 s | SC-007 |
| Casdoor down, inside and beyond the grace | existing token served, a new sign-in fails, then refused with `auth_unavailable`; resumed 3.0 s after Casdoor returned | SC-006 |
| The API's own key copy (cache 10 s, grace 15 s, the door stopped) | served inside the cache and inside the grace; `503 auth_unavailable` beyond it; `401` for a request with no identity; recovered by itself after the door returned | FR-011, plan failure table |
| Logs of the API and the worker | no external token and no JWT-shaped string; none of the 28 tokens of the run in any service's log | SC-009 |
| The web page in Chromium, two tenants, `make authlimit-web-check` | 28 of 28: signed-out box, hidden pickers, Casdoor's own form, no code or token in the address bar or history, token in `sessionStorage` only, a chat turn answered with the signed-in tenant's rows only, the request carrying a Bearer and neither identity header, reload keeps the login, sign-out clears the conversation, a refused token returns to the sign-in box saying so | FR-030 |

**Still unmeasured:** Casdoor's behaviour for a user who belongs to two organizations (the page asks for one); a refresh token in the page (not built: a 7-day access token and sign-in again); the page under a Content-Security-Policy (none is set); production Caddy and compose wiring (T069, not part of this run); behaviour under load.
