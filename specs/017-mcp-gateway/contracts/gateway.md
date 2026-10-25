# Contract: the MCP gateway

What a client, an operator and a reviewer can rely on. Behaviour here is pinned by `tests/mcp/test_gateway*.py`, `tests/core/test_compose_gateway.py` and `make mcp-gateway-smoke`.

## Endpoints (origin = `MCP_GATEWAY_PUBLIC_URL`, default `https://mcp.localhost:8443`)

| Path | What | Audience a token must carry | Scope | Who |
|---|---|---|---|---|
| `POST /ecorp/mcp` | the `ecorp-structured-data` server: `query_employees` | `<origin>/ecorp/mcp` | `ecorp:read` | any tenant with a valid token (its own rows) |
| `POST /ops/mcp` | the `ecorp-ops` server: `fetch_metrics_summary`, `list_recent_incidents` | `<origin>/ops/mcp` | `ops:read` | tenants in `MCP_GATEWAY_OPS_TENANTS` only |
| `GET /.well-known/oauth-protected-resource/ecorp/mcp` (and `/ops/mcp`) | RFC 9728 metadata: `resource`, `authorization_servers` (exactly the configured issuer), `scopes_supported` | none | none | anyone |
| `GET /healthz`, `/readyz` | liveness; readiness (503 when the signing keys cannot be confirmed). **Not routed by the proxy**: for the load balancer inside the network | none | none | the network |

Streamable HTTP, stateless, JSON responses. No other path is served (404).

## Answers

| Request | Status | Notes |
|---|---|---|
| no `Authorization: Bearer` | 401 | `WWW-Authenticate: Bearer error="invalid_token", error_description=..., resource_metadata="<origin>/.well-known/oauth-protected-resource/<path>"` |
| a token that fails any check below | 401 | same header; the reason is a counted, logged class name, never in the response |
| valid token, scope missing | 403 | `error="insufficient_scope"`, names the scope |
| valid token, ops endpoint, tenant not an operator | 403 | JSON `{"error":"forbidden", ...}` |
| any `Host` other than the public one | 421 | before any tool runs |
| a valid, permitted call | 200 | the tool runs under the token's tenant and principal |
| a valid token with no usable tenant or principal claim | 200, tool result `Refused: ...` | the tool refuses; nothing runs unscoped |

## A token is valid for an endpoint iff

signed by a key in the configured JWKS (`kid` found; algorithm `RS256`, from the allow-list not from the token's header); `iss` equals `MCP_GATEWAY_ISSUER` exactly; `aud` contains **that
endpoint's** resource URI; `exp` is in the future (5 s of clock skew) and `sub` is present. A token whose audience is the client id (an authorization request without `resource`) is refused.

## Identity

`tenant` = the token's `MCP_GATEWAY_TENANT_CLAIM` claim (default `owner`, a Casdoor organization), `principal` = `MCP_GATEWAY_PRINCIPAL_CLAIM` (default `sub`). Both must be non-empty text. No tool takes
either as an argument and the launcher's `MCP_TENANT`/`MCP_PRINCIPAL` are never read. Every call logs one `mcp_tool_call` line (server, tool, tenant, principal, outcome, duration).

## Settings (`.env.example`)

`MCP_GATEWAY_PUBLIC_URL`, `MCP_GATEWAY_ISSUER`, `MCP_GATEWAY_JWKS_URL`, `MCP_GATEWAY_TENANT_CLAIM`, `MCP_GATEWAY_PRINCIPAL_CLAIM`, `MCP_GATEWAY_OPS_TENANTS`, `MCP_GATEWAY_JWKS_CACHE_SECONDS`. The gateway
exits at start, saying which, if the issuer or JWKS URL is empty, a URL is not absolute or carries a path where it must not, or plain http is used outside loopback (public URL, issuer) or an
in-network dotless name (JWKS).

## What the authorization server must provide (Casdoor 4.24.0, set by `make mcp-gateway-bootstrap`)

- one organization per tenant and one for the operators; one **application** per organization and server (`mcp-<tenant>`, `mcp-ops`), category `Agent`, type `MCP`, token format `JWT-Standard`,
  one-hour access tokens, grants `authorization_code` and `refresh_token`, the scope (`ecorp:read` / `ops:read`), and the client's fixed callback registered exactly;
- `origin` set to the issuer, so a token's `iss` does not depend on the Host header of whoever asked;
- signing keys at the JWKS URL; PKCE `S256`; dynamic client registration **off** (leave it off: anyone who can reach Casdoor could register a client).

## Registering a client (Claude Code)

```bash
make mcp-gateway-up && make mcp-gateway-bootstrap && make mcp-gateway-ca
export NODE_EXTRA_CA_CERTS=~/.cache/agent-core-demo-mcp-gateway/caddy-root.crt   # before starting Claude Code
claude mcp add --transport http --callback-port 33418 --client-id <printed by the bootstrap> ecorp https://mcp.localhost:8443/ecorp/mcp
# then /mcp in Claude Code, and log in as the printed user
```
A pre-registered client (no secret) because Casdoor has neither Client ID Metadata Documents nor open registration. Not run with Claude Code itself (specs/017 T020).

## Scaling

`docker compose --profile gateway up -d --no-deps --no-recreate --scale mcp-gateway=3 mcp-gateway`. Caddy's `dynamic a` finds every replica; `/readyz` takes one out that cannot check tokens;
`lb_try_duration` re-dials a failed connect on another replica. Nothing is pinned to a replica.

## Moving Casdoor to a newer release

Take the new tag's digest from Docker Hub, change it in `deploy/compose/docker-compose.yml`, `make mcp-gateway-up`, `make mcp-gateway-bootstrap`, then `make mcp-gateway-smoke`. If it fails, read the
failing check against `research.md` M2 first: the audience, the claim set, public clients and the issuer are what this repo depends on.
