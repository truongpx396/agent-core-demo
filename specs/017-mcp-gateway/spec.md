# Spec 017: the published MCP servers over Streamable HTTP, behind OAuth 2.1 (the MCP gateway)

**Status:** built, one PR, stacked on #195 (identity is the launcher's, never an argument) and #197 (mcp 2.x) until they merge. **Scope decision (the owner's words, 2026-10-26):** "an
scalable standalone mcp service to host ... the two mcp servers of the repo like ops or ecorp under auth2.1 that other agents like claude code can also use", with an authorization server
"like casdoor", built and tested locally. The sandbox is deliberately **not** in it (see the constitution check).

## Why

`app/mcp/server.py` and `ops_server.py` speak stdio: a child process of whoever launched it, one client each, no port, an identity taken from that person's environment (#195). That is right
for a desktop assistant on the same machine and wrong for anything else: a remote agent, a team, or more than one replica has no launcher to trust. Spec 009 named the gap (A4: "nothing
authenticates the person who starts a published server") and left it. The MCP authorization specification gives the answer for the HTTP case: the server is an OAuth 2.1 resource server,
the identity is a verified, audience-bound token on every request, and the authorization server is somebody else's job.

## What changes

1. **`app/mcp/gateway.py`**: one ASGI app serving `/ecorp/mcp` (scope `ecorp:read`) and `/ops/mcp` (scope `ops:read`) over Streamable HTTP, stateless. Each is its own OAuth resource: a token's
   audience must be that endpoint's URL (RFC 8707). Health (`/healthz`) and readiness (`/readyz`, 503 when the signing keys cannot be confirmed) are not routed publicly.
2. **`app/mcp/gateway_auth.py`**: `JwksCache` (fetch once, refresh on an unknown `kid` at most once per 30 s, never trust keys older than the TTL), `JwtVerifier` (signature, issuer, audience, expiry;
   an algorithm allow-list; never raises) and `TenantGate` (the operations server admits operator tenants only).
3. **The identity seam** (`app/mcp/identity.py`): `IdentitySource`, with the launcher's environment for stdio and `token_source` (tenant and principal from the verified claims) for the gateway.
   Both servers become `build_server(identity)` factories; the stdio entry points and their tests are unchanged. The gateway never reads `MCP_TENANT`/`MCP_PRINCIPAL` (a test greps for it).
4. **A `gateway` compose profile** (`make mcp-gateway-up`): Casdoor on its own Postgres, the gateway (any number of replicas), and a Caddy with local TLS on `https://mcp.localhost:8443` /
   `https://auth.localhost:8443`. Third-party images pinned by digest; the one published port is loopback.
5. **`scripts/mcp_gateway_bootstrap.py`** (`make mcp-gateway-bootstrap`): the Casdoor organizations (one per tenant, plus the operators'), the applications with their scopes and the fixed Claude
   Code callback, demo users; prints the `claude mcp add` commands. **`scripts/mcp_gateway_smoke.py`** (`make mcp-gateway-smoke`): the end-to-end check as a client does it.

## Requirements

- **FR-001** A request without a valid token for the endpoint it names is a 401 with `WWW-Authenticate: Bearer ... resource_metadata="..."`; a valid token without the endpoint's scope is a 403
  `insufficient_scope` that names it. Neither runs a tool.
- **FR-002** A token is valid for an endpoint only if it is signed by a key in the configured issuer's JWKS with an allow-listed algorithm, has `iss` equal to the configured issuer, `aud`
  containing that endpoint's own resource URI, `exp` in the future and a `sub`. The audience of a token minted without a `resource` parameter is the client id, and is refused.
- **FR-003** The tenant and principal a tool acts for are the token's `owner` and `sub` claims (settings name them). No argument, and no launcher environment, can change them; a token without a
  usable text tenant and principal is refused by the tool, not run unscoped.
- **FR-004** The operations endpoint admits a caller only if the token's tenant is in `MCP_GATEWAY_OPS_TENANTS` (403 otherwise; empty means nobody).
- **FR-005** The gateway does not start without an issuer and a JWKS URL; plain http is accepted only for a loopback public URL or issuer and an in-network (dotless) key fetch.
- **FR-006** The verifier never raises. A token, a claim, an argument or a result never reaches a log; the audit line (`mcp_tool_call`) names server, tool, tenant, principal, outcome and duration.
- **FR-007** It is stateless: any replica answers any request, concurrent callers of different tenants never see each other's identity, and `--scale mcp-gateway=N` behind the Caddyfile load-balances.
- **FR-008** `agent_mcp_gateway_requests_total{server,outcome}` counts ok, invalid_token, jwks_unavailable and forbidden_tenant; `McpGatewayCannotCheckTokens` alerts on the last of the keys.
- **FR-009** Starting the dev compose stack requires no variable to be set (compose interpolates the whole file on every command).

## Success criteria

- **SC-001** The hermetic gateway tests pass (27 end to end in memory with the real SDK middleware and transport, 12 for the key cache and verifier, 23 for configuration; plus 12 on the compose stack and 1 on the alert: 75 new, the default tier 4,345 passed against 4,270 before), and every deliberate bug in the checks is caught: 15 mutants, 15 killed
  (no audience check, no issuer check, no expiry/subject requirement, a huge clock leeway, an open operator gate, a scope parser that raises, no key-refresh rate limit, keys that never go
  stale, no required scope, DNS-rebinding guard off, a stateful transport, ops open to every tenant, a tenant coerced to text, a fallback to the launcher's identity, an argument that moves
  the tenant).
- **SC-002** `make mcp-gateway-smoke` passes on the real stack (Casdoor 4.24.0 on Postgres, Caddy with its own CA, the gateway, the repo's Postgres): 17 checks (see `research.md`).
- **SC-003** Three replicas share traffic without sticky routing (60 of 60 calls, spread about evenly), and killing one under steady traffic failed 0 of 615 calls.

## Constitution check

- **I (tenant isolation, non-negotiable):** the `SecurityCtx` comes only from a verified token (the "stamped once upstream" rule, with the transport as the boundary); a missing or non-text
  claim is a refusal, never a default; the store's `WHERE tenant = %s` is unchanged; the live check reads `ecorp` and `other-co` rows through the real tools and each sees only its own.
- **II (approval for side effects, non-negotiable):** **no `mutating` or `outward` tool is reachable through the gateway.** Both published tools are `read_only`. The sandbox is not behind it:
  its tools are `outward`, the approval gate lives in the agent's graph, and an outside caller does not pass through that graph. Adding a write tool to either server, or exposing the sandbox,
  needs a decision on how approval works for a caller outside the graph, and an amendment to this principle if the answer is "the client's own prompt". **HUMAN**, not decided here.
- **III (fixed, typed tools):** the same two factories build the stdio and the HTTP servers; the tools, their schemas and their descriptions are one definition (a test compares the two lists).
- **IV (exactly-once):** no side-effect tool, so nothing to key. The one retry in the path (Caddy re-dialling another replica after a failed connect) applies to read-only calls only.
- **V (bounded, observable failure):** 5 s key-fetch timeout; at most one key refresh per 30 s however many tokens arrive; keys older than the TTL are not trusted; the metric, the alert and
  `/readyz` above. The honest gap is that a key outage looks like an expired login to a client (401).
- **VI (untrusted content is data):** a bearer token is untrusted input, verified before anything else and never logged; arguments and results are not logged either.
- **VII (real backends):** the unit tier replaces only the identity provider; the real one is exercised by `make mcp-gateway-smoke` and by the live run recorded in `research.md`.
- **VIII (why-first, honest gaps):** this spec, the contract, `research.md` and GRAPH_PATTERNS.md pattern 82 say what was measured and what was not.

## Not built, and why

- **A real Claude Code login.** The OAuth browser step cannot be driven from here. The smoke test performs the identical sequence a client does (discovery, public-client PKCE with `resource`,
  bearer calls), but Claude Code itself has not been connected. **HUMAN** (T020).
- **Production wiring.** Real DNS and TLS (ACME), secret management, a production identity provider and a deployment manifest. The compose profile is for local use; the gateway needs only an
  issuer URL and a JWKS URL, and nothing in it is Casdoor-specific.
- **Per-tenant rate limits and a quota.** A valid token can call as often as the process allows.
- **Stale-key grace.** If the authorization server is down longer than the key TTL the gateway refuses everyone (fail closed) rather than trusting keys it cannot confirm.
- **Token revocation.** Verification is offline: an access token is good until it expires (an hour). Casdoor offers introspection; using it would put the authorization server in the request path.
- **The sandbox, and any write tool** (above).
- **Automation of many tenants.** One Casdoor organization and one application per tenant is what the bootstrap creates; at tens of tenants that wants generation from a list, not flags.
- **Hardening of Casdoor, Caddy and the database** beyond `no-new-privileges`: nobody has checked their write paths (the repo's rule is not to guess).
