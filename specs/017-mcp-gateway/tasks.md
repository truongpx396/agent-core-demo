# Tasks: spec 017

- [X] T001 `app/mcp/identity.py`: `IdentitySource`, `IdentityMissing` (+ launcher and token subclasses), `launcher_source`, `token_source`; `audit.audited(server, identity)`
- [X] T002 `server.py` / `ops_server.py`: `build_server(identity, refusal=, **server_kwargs)`; the stdio `mcp` and `main()` unchanged (27 existing tests unchanged)
- [X] T003 `app/mcp/gateway_auth.py`: `JwksCache`, `JwtVerifier`, `TenantGate`; `agent_mcp_gateway_requests_total`
- [X] T004 `app/mcp/gateway.py`: `GatewayConfig` (fail closed), per-endpoint resource servers, exact-path dispatch, lifespans, `/healthz`, `/readyz`, `main()`; settings and `.env.example`
- [X] T005 Tests: end to end in memory (`tests/mcp/test_gateway.py`, 27), key cache and verifier with a movable clock (`test_gateway_keys.py`, 12), configuration (`test_gateway_config.py`, 23); mutation-checked (15 of 15 killed)
- [X] T006 Compose profile `gateway` (Casdoor, its Postgres, the gateway, Caddy), `deploy/caddy/Caddyfile.gateway`, Makefile targets; `tests/core/test_compose_gateway.py`; `mcp-gateway` added to the hardening test's list
- [X] T007 `scripts/mcp_gateway_bootstrap.py` (idempotent, run twice to prove it) and `scripts/mcp_gateway_smoke.py` (17 checks, throwaway users deleted)
- [X] T008 Alert `McpGatewayCannotCheckTokens` and its test
- [X] T009 Live verification on the real stack (isolated project): smoke 17/17, three replicas, a replica stopped and killed under traffic, logs searched for secrets, runtime posture read back
- [X] T010 Docs: this spec, `contracts/gateway.md`, `research.md`; GRAPH_PATTERNS.md pattern 82, ENGINEERING_PATTERNS.md pattern 83; README; CLAUDE.md
- [ ] T020 **HUMAN** Connect the real Claude Code: `make mcp-gateway-up && make mcp-gateway-bootstrap && make mcp-gateway-ca`, `claude mcp add ...` as `contracts/gateway.md` says, `/mcp`, log in, call a tool. Record what happens (the CA variable, the fixed callback, whether it asks for a secret)
- [ ] T021 **HUMAN** Decide the production identity provider, DNS and TLS, and where the generated secrets live; the gateway only needs `MCP_GATEWAY_ISSUER` and `MCP_GATEWAY_JWKS_URL`
- [ ] T022 **HUMAN** Decide whether any write tool, or the sandbox, may ever be reachable by a caller outside the agent's graph (Principle II). Not built until decided
- [X] T023 Per-tenant rate limits and a quota on the gateway — **closed by spec 018**: the MCP routes go through the authlimit service in limit-only mode (per-tenant and per-person buckets, and revocation), `specs/018-authlimit-edge-auth/`
- [ ] T024 Generate the Casdoor organizations and applications from a tenant list when there are more than a handful
