# Feature Specification: Edge authentication and rate limiting for every public route (authlimit)

**Feature Branch**: `feat/018-authlimit-edge-auth`

**Created**: 2026-10-27

**Status**: Draft

**Input**: The maintainer's brief of 2026-10-27: a self-built service that the front door (Caddy) asks about every request, verifying the caller's token and enforcing per-tenant and per-person rate limits, with Casdoor as the single authorization server; testable locally with no Cloudflare; delivered in exactly two PRs. The decisions in the brief are recorded under "Decisions" below.

## Why

Today the chat API does no authentication. `app/api/deps.py` reads `X-Tenant-Id` and `X-Principal-Id` from the request and trusts them, on the stated assumption that "production sits behind a gateway that authenticates the caller and sets these headers itself". No such gateway exists. The web UI even ships a tenant drop-down the user can edit. The per-tenant rate limit (`app/api/rate_limit.py`) counts on that same unauthenticated header, so a client can rotate tenant ids to escape it, and a client can act as any tenant simply by naming it. Spec 017 built the first verified identity in the repo, but only for the MCP servers, and left per-tenant rate limits on it as task T023.

This feature builds the missing gateway layer: one place that proves who is calling and meters how much they may call, in front of the chat API and the MCP routes, using the same authorization server for both. It follows the pattern large systems use: verify at the edge, pass a verified identity inward, and let each service re-check what it receives. The chat API never learns to read the external token; the MCP gateway keeps verifying it itself, because the MCP authorization specification requires that.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A signed-in person chats and the API knows exactly who they are (Priority: P1)

A person signs in through the authorization server, opens the web UI and sends a chat message. The request reaches the API carrying a proven tenant and principal, taken from their token and from nothing they typed. They see only their own tenant's data.

**Why this priority**: This is the whole point. Until it holds, tenant isolation (constitution Principle I) rests on a header anyone can set.

**Independent Test**: Run the local stack, sign in as a user of tenant `ecorp` and as a user of tenant `other-co`, ask each the same question about stored records, and check that each sees only their own tenant's rows. Repeat after sending forged tenant headers: the answer does not change.

**Acceptance Scenarios**:

1. **Given** a valid access token for tenant A, **When** the person sends a chat message through the front door, **Then** the turn runs as tenant A and the person's own principal, and the answer contains only tenant A's data.
2. **Given** the same valid token and a request that also carries a different tenant id, principal id or identity header of the client's own making, **When** the request is sent, **Then** the request runs as tenant A and the extra headers have no effect.
3. **Given** no token at all, **When** any authenticated route is called, **Then** the request is refused at the front door and never reaches the API.

---

### User Story 2 - Bad tokens are stopped before they reach the API (Priority: P1)

A request with a missing, malformed, expired, unsigned, wrongly signed, wrong-issuer, wrong-audience, wrong-scope, over-long-lifetime or revoked token is refused at the front door with a standard error body, and the API is never called.

**Why this priority**: A verifier is only as good as the tokens it refuses; this is the security-critical half of the service.

**Independent Test**: Run one shared set of token vectors (valid and invalid, each with its expected outcome) through the new service and through the existing MCP gateway verifier, and require that they agree on every vector.

**Acceptance Scenarios**:

1. **Given** each invalid-token case above, **When** the request is made, **Then** the response is a 401 (or 403 for a valid token lacking the scope) with the repository's error envelope, and no request reaches the API.
2. **Given** a token the authorization server minted for a different resource, **When** it is presented, **Then** it is refused even though its signature is valid.
3. **Given** a token that claims a lifetime longer than the configured cap, **When** it is presented, **Then** it is refused, so a misconfigured authorization server cannot silently mint immortal tokens.

---

### User Story 3 - Fair, visible rate limits per tenant and per person (Priority: P2)

A tenant or a single person that sends too much is told to slow down, with how long to wait; other tenants and other people in the same tenant are unaffected. Stopping a runaway turn is never throttled.

**Why this priority**: It protects the shared work queue and the model budget from one noisy caller, and it replaces a limit that could be evaded.

**Independent Test**: Drive one tenant past its limit and watch it receive 429s with a wait time, while a second tenant and cancel requests keep succeeding; then switch the limiter to observation mode and confirm nothing is blocked but the would-be blocks are counted.

**Acceptance Scenarios**:

1. **Given** a tenant at its limit, **When** any of its people sends a turn-creating request, **Then** the response is 429 with a wait time and the API is not called.
2. **Given** one person at their own limit inside a tenant that is not, **When** a colleague sends a request, **Then** the colleague is served.
3. **Given** a throttled tenant, **When** they cancel a running turn, call a read route or ask for health, **Then** those succeed (these routes are never limited).
4. **Given** observation mode, **When** a caller exceeds the limit, **Then** the request is served, the would-be block is counted and logged, and nothing is refused.
5. **Given** the limit store is unreachable, **When** requests arrive, **Then** they are served (the limiter fails open), a counter records it and an alert fires.

---

### User Story 4 - The front door survives the failures it will meet (Priority: P2)

Two copies of the service run behind the front door. Losing one, rotating a signing key, or losing the authorization server for a while does not lock people out within the declared limits, and it never lets an unverified request through.

**Why this priority**: The service is on the path of every request; its availability is the API's availability.

**Independent Test**: Stop one replica under steady traffic; rotate the internal signing key under traffic; stop the authorization server inside and then beyond the grace period; observe the outcomes in the local stack.

**Acceptance Scenarios**:

1. **Given** two replicas and steady traffic, **When** one is stopped or killed, **Then** no request fails beyond those in flight on that replica, and the front door stops sending to it within its health-check interval.
2. **Given** a key rotation under traffic, **When** the new internal key becomes current, **Then** no request fails: services accept both the old and the new key during the overlap window.
3. **Given** the authorization server is down for less than the grace period, **When** a person with an unexpired token calls, **Then** they are served from the last confirmed keys, a metric shows the keys are stale and an alert fires.
4. **Given** the authorization server stays down beyond the grace period, **When** any request arrives, **Then** it is refused (fail closed) with a clear operator-facing signal, and service resumes within one refresh interval after the server returns.
5. **Given** the authorization server rotates its own signing key, **When** tokens signed by the new key arrive, **Then** they are accepted after at most one rate-limited key refresh.

---

### User Story 5 - The MCP routes share the same rate limits and revocation (Priority: P2)

The existing MCP servers keep verifying the external token themselves (the MCP authorization specification requires it), but the front door also meters those routes by tenant and person and honours revocation, so there is one rate-limit and revocation policy point.

**Why this priority**: It closes spec 017's open task T023 without a second limiter and keeps revocation consistent.

**Independent Test**: Through the front door, call an MCP route with a valid token until throttled; revoke the person and confirm both a chat call and an MCP call stop.

**Acceptance Scenarios**:

1. **Given** an MCP route and a valid token, **When** the tenant exceeds its limit, **Then** the response is 429 with a wait time.
2. **Given** a request to an MCP route with no or an invalid token, **When** it arrives, **Then** the MCP gateway answers it with its own 401 and discovery metadata exactly as today (the front door does not replace that answer).
3. **Given** a revoked person, **When** they call an MCP route, **Then** the call is refused with the same discovery-bearing 401.

---

### User Story 6 - An operator can cut off a person, and bound the cost of a long-lived token (Priority: P3)

Access tokens last seven days by default and are verified without asking the authorization server, so a stolen or abandoned token would otherwise stay good for a week. An operator can revoke a person so every token issued to them before that moment stops working everywhere within seconds, and the person can sign in again afterwards. The lifetime a token may claim is capped, and a shorter lifetime (with refresh) is the recommended production setting.

**Why this priority**: Offline verification has a cost; this is how the cost is bounded the way a professional team bounds it.

**Independent Test**: Sign in, call successfully, revoke the person, call again and be refused on every replica within seconds, sign in again and call successfully.

**Acceptance Scenarios**:

1. **Given** a signed-in person, **When** an operator revokes them, **Then** their existing tokens are refused on all replicas within 5 seconds.
2. **Given** a revoked person, **When** they sign in again and receive a new token, **Then** the new token works.
3. **Given** the revocation store is unreachable, **When** requests arrive, **Then** each replica keeps enforcing the last revocation list it received for a bounded time, and after that ceiling requests are refused by default (a security check fails closed), with a metric and an alert; the operator may configure the opposite.

---

### User Story 7 - Operators can see and trust what the service does (Priority: P3)

Every outcome (allowed, refused and why, limited, key problems, store problems) is counted; the conditions that leave people unaware have alert rules; logs carry metadata and never a token, key or secret; a written threat model covers the new security-critical surface.

**Why this priority**: Constitution Principle V: no degradation may be silent.

**Independent Test**: After the end-to-end run, scan every service log for token fragments and key material (none), and check each metric and alert in the contract exists and fires in a forced-failure test.

**Acceptance Scenarios**:

1. **Given** the end-to-end run, **When** logs are searched for a token, a key or a password, **Then** none appears.
2. **Given** each degraded condition (stale keys, keys unavailable, limiter store down, revocation store down, replica down, header-trust mode active), **When** it is forced, **Then** its metric moves and its alert rule fires.

---

### Edge Cases

- A token is valid when the request arrives and expires while a long streamed answer is still being sent: the stream is not cut (the check happens once, at the start of the request).
- Clock skew between the service and the authorization server: a small bounded leeway is allowed; a large one is not.
- An internal identity captured from one response is replayed later or sent to a different service: it is refused after its short lifetime and is only valid for the API.
- An internal identity is presented as an external token, or an external token is presented where an internal identity is expected: both are refused (the two use different keys, issuers and types).
- A client sends the internal-identity header itself, sends it twice, or varies its case: the client's copy never survives, whether or not the service returns a header of its own (verified on Caddy 2.11.7 on 2026-10-27 and re-checked by the smoke).
- The caller's `Authorization` header reaches the service (verified) and, unless removed, also reaches the API; the front door removes it after the check (FR-037), so the API never holds the external token.
- The service is completely unreachable: the front door answers 502 without calling the API (verified), which is the fail-closed behaviour wanted.
- A token without an issue time, or with a lifetime longer than the cap: refused.
- Two replicas see the same request burst: the limit is shared (counted once in total, not once per replica).
- The key-fetch address in configuration is plain http: accepted only for loopback or the in-network name, as in spec 017.
- A huge request volume of invalid tokens: each costs one cheap verification and never reaches the limit store or the API; only the key refresh is rate-limited, as in spec 017.
- A person with a valid token for a tenant that has no applications registered for the API: refused as an unknown audience, never defaulted to another tenant.
- Cancel and read routes are verified like any other authenticated route (they act on a tenant's data and need the identity) but are never rate-limited, which keeps the existing "stopping a runaway turn is never throttled" rule. Health, readiness, static web assets and the payment webhook are public: this service does not verify or limit them (the webhook authenticates by its signature and stays on its existing per-address limit).

## Requirements *(mandatory)*

### Functional Requirements

**Verification of the external token**

- **FR-001**: Every request to an authenticated route MUST be checked at the front door before it reaches the API. A request without a valid token is refused there.
- **FR-002**: A token MUST be accepted only if all of these hold: signed by a key published by the configured authorization server using an allow-listed algorithm (the algorithm comes from the allow-list, never from the token); issuer equals the configured issuer; audience contains the API's own resource identifier; expiry is in the future; an issue time and a subject are present; the required scope is present; the claimed lifetime does not exceed the configured cap.
- **FR-003**: The verifier MUST NOT fail by raising or crashing on any input; every malformed input is a refusal with a counted reason that is never the token.
- **FR-004**: The tenant and the principal MUST come only from the verified claims named in settings (today the organization and the subject), and a token without usable text for both MUST be refused, never defaulted.
- **FR-005**: The authorization server's signing keys MUST be fetched with a bounded timeout, cached, and refreshed on an unknown key id at most once per a configured interval however many tokens arrive.

**Internal identity**

- **FR-006**: After a successful check, the service MUST give the front door a signed internal identity (tenant, principal, scopes, issued-at, short expiry, unique id, intended recipient) that the front door puts on the request to the API.
- **FR-007**: The internal identity MUST be signed with an asymmetric key. The private key MUST exist only inside the service; services that consume the identity hold public keys only, so no consumer can mint one.
- **FR-008**: The internal identity's lifetime MUST be very short (default 30 seconds, never more than 120) and its key, issuer and type MUST differ from the external token's so one can never be accepted as the other.
- **FR-009**: The signing key MUST be replaceable without downtime: a new key is published to consumers before it is first used, the old one stays accepted for an overlap window after its last use, and every key carries an identifier.
- **FR-010**: The API MUST accept an identity only from a valid internal identity (in identity mode) and MUST ignore every client-supplied identity header. A missing or invalid internal identity is a 401, never a default tenant (constitution Principle I).
- **FR-011**: The API MUST learn the current internal public keys without a redeploy and MUST keep working through a key rotation.

**Rate limiting**

- **FR-012**: The service MUST enforce a token-bucket limit per tenant and per principal, both keyed only on verified claims; a turn-creating request is admitted only if both buckets have a token. Each bucket's capacity and refill rate are configurable per route class.
- **FR-013**: The limit state MUST be shared across replicas, so the total is counted once.
- **FR-014**: A refused request MUST carry a 429 status, a wait time, and the repository's error envelope.
- **FR-015**: Cancel, read, health, readiness, static assets and the payment webhook MUST NOT be limited by this service; the route classes MUST be a table in configuration, not code scattered across handlers. A route the table does not list is authenticated and limited like a turn (fail safe), counted under its own label, and a test that walks every route the API serves fails on an unlisted one, so the table cannot drift from the API.
- **FR-016**: The limiter MUST support an observation mode that never blocks, counts and logs what it would have blocked, and an enforcement mode; the mode is configurable and documented as the rollout order (observe first).
- **FR-017**: If the limit store is unreachable or slow beyond a bounded ceiling, requests MUST be served (fail open), with a counter and an alert; the limiter MUST NOT stall a request waiting on it.
- **FR-018**: The limiter's keys MUST live under their own prefix in the existing store, so its traffic and eviction cannot collide with the work queue's.

**Revocation and token lifetime**

- **FR-019**: An operator MUST be able to revoke a person: every token issued to them before the revocation moment is refused on every replica within 5 seconds, and tokens issued afterward work.
- **FR-020**: A revocation record MUST expire by itself once no token issued before it could still be valid (the configured lifetime cap).
- **FR-021**: Each replica MUST hold a copy of the revocation list that it refreshes at a short interval (so FR-019's 5 seconds holds without a store lookup on every request). If the store cannot be reached, the replica keeps enforcing its last copy until the copy is older than a ceiling (default 30 seconds); after that, requests MUST be refused by default, with a counter and an alert. The opposite policy (serve) MUST be selectable by configuration and documented as the trade-off. Because the revocation copy is local, a store outage never stalls a request.
- **FR-022**: The access-token lifetime of every application the bootstrap creates MUST default to seven days and be configurable (this changes spec 017's one-hour default for the MCP applications; revocation now covers them, FR-026); the cap in FR-002 MUST default to the same value and be configurable independently, so an operator can tighten it. The production recommendation (short access token with refresh) MUST be documented.

**Failure behaviour**

- **FR-023**: If the authorization server's keys cannot be refreshed, the service MUST keep serving from the last confirmed keys for a bounded grace period (default 6 hours, configurable), raise a metric and an alert at the first failed refresh, and refuse every request (fail closed) after the grace ends.
- **FR-024**: Readiness MUST report not-ready when the service has no usable external keys (beyond the grace) or no signing key, so the front door stops sending to that replica; liveness MUST be independent of the authorization server.
- **FR-025**: Two replicas MUST run by default, share no in-process state that matters, and sit behind the front door's active health checks with a retry on a failed connect, as the MCP gateway does.

**MCP routes**

- **FR-026**: On the MCP routes the service MUST run in a mode that verifies the token's signature, issuer and expiry only to identify the tenant and person, applies the same limits and revocation, and passes requests with no or an invalid token through to the MCP gateway unchanged, so that the gateway's own 401 and discovery answer is preserved. For a revoked person it MUST produce the same discovery-bearing 401.
- **FR-027**: The MCP gateway MUST continue to verify the external token itself; nothing in this feature removes or weakens that check.

**API side**

- **FR-028**: The API MUST have an explicit authentication mode: identity mode (internal identity required) and header mode (today's behaviour). Identity mode is the default; header mode MUST be chosen explicitly, is for tests and local development without the front door, and MUST announce itself in the log and in a metric with an alert rule, so a deployment left in it is visible. Identity mode without the key location it needs MUST stop the API from starting.
- **FR-029**: The in-API per-tenant request limiter MUST apply only in header mode (it is the fallback when no front door is present). In identity mode it MUST be off, so one request is never counted twice and there is one rate-limit policy point. The payment webhook's per-address limit is unchanged in both modes. (A per-tenant in-flight turn cap in the API is a separate, later change.)
- **FR-030**: The web UI MUST sign the person in through the authorization server (authorization code with PKCE, public client), send the access token with every API call, remove the editable tenant selector in identity mode, and handle expiry by signing in again (or refreshing, where the server issued a refresh token).

**Observability, secrets, verification**

- **FR-031**: Metrics MUST count requests by outcome and reason, limiter decisions by mode, key state and age, store failures by store, replica readiness, and request latency; every degrade path in FR-017, FR-021, FR-023 and FR-028 MUST have an alert rule in `observability/prometheus/alerts.yml` with a test.
- **FR-032**: No token, key, password or claim value beyond tenant and principal ids MUST appear in a log, metric label or error body. The signing key MUST be supplied as a mounted secret, never baked into an image or committed, and its absence MUST stop the service from starting.
- **FR-033**: The service's error bodies MUST use the repository's `{code, message, details}` envelope with codes registered in `ErrorCode`.
- **FR-034**: One shared set of token vectors (valid and invalid, each with an expected outcome and an injected clock) MUST be run by both the new verifier's tests and the MCP gateway verifier's tests, and both MUST agree on every vector.
- **FR-035**: A written threat model MUST cover at least: header spoofing, algorithm and key confusion, cross-service token replay, internal-identity replay and confusion, key compromise and rotation, denial of service on key refresh, authorization-server and store outages, revocation gaps, log leakage, and the configuration of key-fetch addresses.
- **FR-036**: The whole feature MUST be exercisable on one machine with the local authorization server, the front door and the existing Redis, with no Cloudflare and no other external service, through one documented command sequence and one end-to-end smoke check.
- **FR-037**: On the chat-API routes the front door MUST remove the caller's `Authorization` header after the check and before the request reaches the API, so the external token never reaches, and can never be logged by, the API.

### Key Entities

- **External access token**: the signed token the authorization server issues after login; carries issuer, audience, expiry, issue time, subject, organization (tenant) and scope.
- **Internal identity**: a short-lived signed assertion minted by the service after a successful check; carries tenant, principal, scopes, a unique id and its own short expiry; consumable only by the API.
- **External key set**: the authorization server's public signing keys, cached with a freshness age and a grace deadline.
- **Internal key set**: the service's signing keys, each with an identifier, a state (published, current, retiring) and the public halves served to consumers.
- **Rate bucket**: a token bucket for one tenant or one principal on one route class, with capacity and refill rate, shared across replicas.
- **Revocation record**: "tokens issued to this person before this moment are void", expiring on its own.
- **Route class**: a named group of routes (authenticated and limited, authenticated and unlimited, public, MCP, webhook) that decides what the service does. "Authenticated route" in this spec means every route not in the public or webhook class.
- **Replica**: one running copy of the service; stateless apart from caches.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Of the shared token vectors (target: at least 25 covering every refusal reason in FR-002), 100% get the expected outcome from the new verifier, and the MCP gateway verifier agrees on every one; no refused request reaches the API in the end-to-end run.
- **SC-002**: With two tenants signed in on the local stack, each sees only its own data through the front door, and 0 of at least 8 spoofing variants (forged tenant, principal, internal-identity header, duplicate headers, header casing) change the identity a turn runs as.
- **SC-003**: A tenant driven past its limit receives 429 with a wait time on the next request while another tenant is unaffected; cancel and read requests succeed 100% of the time during the throttle; in observation mode 0 requests are blocked and the would-block count matches.
- **SC-004**: With two replicas under steady traffic, stopping one causes at most the in-flight requests on that replica to fail (the MCP gateway's reference result was 0 of 615 calls), and traffic is spread across both while both are up.
- **SC-005**: During an internal key rotation under traffic, 0 requests fail; after a Casdoor key rotation, new tokens are accepted within one minute.
- **SC-006**: With the authorization server stopped, existing tokens keep working until the grace ends and the stale-keys alert fires at the first failed refresh; after the grace every request is refused; service resumes within one refresh interval of recovery.
- **SC-007**: A revoked person is refused on both replicas within 5 seconds, on both a chat route and an MCP route, and a fresh sign-in works.
- **SC-008**: The added time per authenticated request, measured on the local stack and recorded in the research notes, has a median under 10 ms and a 95th percentile under 50 ms. These are proposed targets, not measured results.
- **SC-009**: A search of every service's logs after the end-to-end run finds no token, key or password, and each alert rule in FR-031 fires in its forced-failure test.
- **SC-010**: The default hermetic Python suite still passes with its fixtures selecting header mode, and nothing else in it changes; the new service's own suite passes; every deliberate defect in a mutation list for the verifier and limiter (for example: no audience check, no lifetime cap, algorithm taken from the token, limiter keyed on an unverified claim, revocation checked after the limiter, internal identity accepted as an external token) is caught by a test.
- **SC-011**: Delivery is two PRs. Each one's hand-written line count is recorded in its description; where it exceeds the repository's guidance the description says so and names the pieces that would be split off first.

## Assumptions

- **A1 (production is not running, so the secure default can land now).** The maintainer confirmed on 2026-10-27 that the app is not running in production yet and that the existing files may be refactored as needed. `.github/workflows/deploy.yml` deploys on every push to `main` and the production compose file has no authorization server, so this would have been an outage hazard for a live system; for one that is not live it is not, and the API's mode therefore defaults to identity mode (FR-028) and the redundant in-API limiter is gated rather than kept in parallel (FR-029). The production compose file and Caddyfile are wired for identity mode in PR 2 (cuttable, see Delivery) but no identity provider is provisioned by this feature: until one is, a production API started from them refuses unauthenticated requests, which is the right failure for something not yet live. Provisioning a production identity provider remains spec 017's T021 (HUMAN).
- **A2** The brief's "open decisions" are settled as: asymmetric internal signing (FR-007); a 30-second internal identity lifetime (FR-008); key rotation with an overlap window of 10 minutes by default (FR-009); a 6-hour stale-key grace (FR-023); verifier fails closed, limiter fails open, revocation lookup fails closed (FR-017, FR-021, FR-023); observation mode before enforcement (FR-016). All are configurable.
- **A3** Default limits keep parity with today: a tenant bucket refilling at 30 requests a minute (the current `RATE_LIMIT_PER_MINUTE`) with a small burst, and a per-person bucket at a third of that. Both are configurable per route class and are expected to be tuned from observation mode.
- **A4** Casdoor 4.24.0 behaves as measured in spec 017's research: tokens in `JWT-Standard` form, the organization as the tenant and the subject as the principal, an issuer pinned with its `origin` setting, a public client with PKCE, the `resource` parameter setting the audience, and no check that the `resource` is legitimate (so audience and scope are both required).
- **A5** The API needs its own resource identifier and scope in Casdoor, and a registered redirect address for the web UI, created by an extension of the existing bootstrap script (in PR 1, so the service can be proven with real tokens before the API changes), for each demo tenant.
- **A6** The existing Redis is used (workers, queue and cache share it); the limiter and revocation keys have their own prefix and bounded waits. Moving them to a separate instance is a deployment choice, not part of this feature.
- **A7** Token expiry is checked once per request; a streamed answer already started is never cut by expiry.
- **A8** The front door is Caddy and its `forward_auth` handler. Measured on Caddy 2.11.7 on 2026-10-27 with a throwaway auth service and upstream (recorded in `research.md`): the caller's `Authorization` header and the original method and URI reach the auth service, which receives a bodiless GET; a client-sent copy of the identity header never reaches the upstream, with or without a header from the auth service; a 401 or 429 from the auth service, with its `WWW-Authenticate` or `Retry-After`, reaches the client and the upstream is not called; `lb_try_duration` and `dynamic a` work inside `forward_auth` (20 of 20 requests succeeded with one dead replica and a retry window, 7 to 9 of 20 without); with no auth service reachable the answer is 502 and the upstream is not called.
- **A9** Identity mode is the default (A1), so header mode is chosen explicitly where it is wanted: the hermetic test fixtures and `.env.example` (for running the API directly, without the front door) set it. An existing developer's own `.env` needs `AUTH_MODE=headers` to keep running the API directly, which the PR description says.
- **A10** Cloudflare, per-address limits, and protection of the authorization server's own login endpoints from floods are a later step; until then an unauthenticated flood is limited only by what the machine can absorb (disclosed under "Not built").

## Decisions (made by the maintainer, 2026-10-27)

1. The service is written in **Go**, runs **2 replicas by default**, is stateless, and keeps its shared state in the **existing Redis**. It lives in its own directory with its own module, tests, image and CI job.
2. For the chat API the front door asks the service through Caddy's **`forward_auth`**; the service verifies the external token offline, limits per tenant and per person, and returns a signed internal identity that Caddy copies onto the request. The Python API verifies only that identity.
3. **`mcp-gateway` keeps verifying the external token** (MCP authorization specification, spec 017); the MCP routes also go through the service in limit-only mode (closes spec 017 T023).
4. **Cloudflare is out of scope**; everything is testable locally with Casdoor, Caddy and Redis.
5. The **access-token lifetime defaults to 7 days** and is configurable; the resulting revocation gap is bounded by FR-019 to FR-022.
6. **Two PRs, exactly** (see "Delivery").
7. **The app is not running in production yet** (2026-10-27), so existing files, including the API's identity handling, the in-API limiter and the Casdoor bootstrap defaults, may be refactored as this needs (A1).

## Constitution check

- **I, fail-closed tenant isolation (non-negotiable):** the `SecurityCtx` is stamped once, from a verified identity, in the API's single identity dependency; a missing or invalid identity is a 401, never a default tenant (FR-004, FR-010). The store-level `WHERE tenant = %s` is unchanged. A test proves two tenants cannot see each other's rows through the front door (SC-002).
- **II, III, IV (approval, typed tools, exactly-once):** not touched. No tool is added or changed, and nothing here is a side effect on tenant data. The only retry in the path is the front door re-dialling a different replica after a failed connect, which happens before any request is delivered.
- **V, bounded and observable failure:** every wait has a ceiling (key fetch timeout, store call ceiling, grace period, identity lifetime, revocation expiry); each dependency failure has a stated policy (FR-017, FR-021, FR-023), a metric and an alert (FR-031); logs carry metadata only (FR-032); errors use the `ErrorCode` envelope (FR-033).
- **VI, untrusted content is data:** the bearer token and every header from a client are untrusted input; they are verified first and never logged.
- **VII, test discipline:** the Python default suite stays hermetic and unchanged in header mode; the new service has its own hermetic suite; the live behaviour is proven by an end-to-end smoke on the real stack, and any pytest that needs it carries a marker and self-skips without Docker.
- **VIII, why-first documentation:** the contract, a research note with measured behaviour, the threat model and numbered patterns (taken at write time) record the reasons and the honest gaps below.
- **Architecture and technology constraints:** the constitution's "Runtime: Python 3.13" line describes the app and does not allow for a service in another language. **A MINOR amendment (1.4.0) is proposed in PR 1 through `/speckit-constitution`**, adding a rule for non-Python services in the style of "Self-contained packages": own directory, no dependency on the app's code, the contract and shared vectors as the only coupling, a test that fails the build if that is crossed. The amendment needs the maintainer's sign-off (**HUMAN**); until it lands, the deviation is recorded in the plan's Complexity Tracking table.

## Delivery: exactly two implementation PRs

This specification, its plan, research, contract, threat model and task list land first, on their own, in a documentation-only PR (no code), so the design can be reviewed before it is built. The implementation is then exactly two PRs. The repository's guidance is about 400 hand-written lines per PR with about 1,000 as the ceiling. The brief fixes two PRs, and an honest first estimate is that each will exceed that ceiling (the estimate, to be replaced by counts, is 1,500 to 2,000 lines for PR 1 and 1,200 to 1,800 for PR 2, tests included). The pieces that would be split off first if review demands it are listed with each.

- **PR 1, the service on its own.** Verification, limiting, internal-identity minting and key rotation, revocation (including the operator command), stale-key grace, health and readiness, metrics and alert rules, the shared token vectors, the threat model, the contract and research notes, its image, its test target and CI job, the Casdoor bootstrap's API application (resource, scope, redirect URI) with the seven-day default, the local compose profile with two replicas and the front-door site with `forward_auth` (chat routes) and limit-only mode (MCP routes), proven against a stub upstream that echoes the headers it receives (the chat routes) and against the real MCP gateway (the MCP routes). Includes the constitution amendment proposal. *First to split off:* the operator revoke command and the constitution amendment.
- **PR 2, the Python side.** The API's explicit authentication mode (identity by default) and its use of the internal identity with key rotation, the in-API limiter gated to header mode, the new error codes, the route-table drift test, the web UI sign-in, the MCP gateway running the shared vectors, the end-to-end smoke covering sign-in, refusal, 429, replica kill, authorization-server outage inside and outside the grace, key rotation, token expiry and revocation, documentation and patterns, the header-mode alert, and the production compose file and Caddyfile wired for identity mode. *First to split off:* the production wiring, then refresh handling in the web UI.

## Not built, and why

- **Cloudflare and every per-address limit** (decision 4). Until that step, floods of unauthenticated requests, including against the authorization server's login endpoints, are not limited by this feature. I have not checked whether Casdoor throttles logins itself.
- **A production identity provider** (A1): the production files are wired for identity mode, but nothing here provisions Casdoor (or another provider) for production, creates its tenants or manages its secrets. Spec 017 T021 (HUMAN).
- **A per-tenant in-flight turn cap in the API**, the limit a request-rate bucket cannot express for long streamed turns. Named in the discussion that led to this spec; a separate change.
- **Tenant-wide revocation**, token introspection against the authorization server, and mutual TLS between the front door and the service. Revocation by person covers the offboarding case; the others are named, not needed yet.
- **A hardware-backed or managed key store** for the internal signing key: locally it is a mounted file; production secret management is part of the production step.
- **Refresh handling in the web UI** is a "should" (FR-030) and the first thing cut if PR 2 grows.
- **Dark-launch of authentication itself.** Only the limiter has an observation mode; token verification is enforced whenever identity mode is on, because an observe-only verifier would be an open door.
