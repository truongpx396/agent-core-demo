# Research for spec 017: what was measured, and how

Everything below was observed on 2026-10-25/26, on the maintainer's arm64 Mac, against **Casdoor 4.24.0** (`docker.io/casbin/casdoor:v4.24.0`, `sha256:b7a2005518...`), **mcp 2.3.0**, **PyJWT 2.14**,
Caddy 2.11.7 and Postgres 16. The claims this repo depends on from a third party are measured behaviour of those versions, which is why the Casdoor digest is pinned and `make
mcp-gateway-smoke` exists: run it after moving the digest.

## M1. What the MCP authorization specification asks of a server (draft, fetched 2026-10-25)

A protected MCP server is an OAuth 2.1 **resource server**. It MUST publish Protected Resource Metadata (RFC 9728) and answer an unauthenticated request with a 401 whose `WWW-Authenticate`
carries `resource_metadata`; MUST validate that a token was issued **for it** as the audience (RFC 8707) and MUST NOT accept or pass on any other token; answers `403 insufficient_scope` for a
valid token without the needed scope. The authorization server is out of its scope. Clients SHOULD support Client ID Metadata Documents; dynamic client registration is deprecated. All
authorization-server endpoints MUST be https; redirect URIs MUST be `localhost` or https. stdio servers SHOULD NOT follow the specification and take credentials from the environment.

## M2. Casdoor 4.24.0, measured

| Question | Answer |
|---|---|
| Discovery | both `/.well-known/openid-configuration` and `/.well-known/oauth-authorization-server`; PKCE `S256` advertised; RFC 9207 `iss` parameter supported; introspection endpoint present |
| Client ID Metadata Documents | **not supported** (`client_id_metadata_document_supported: false`), contradicting a vendor blog post |
| Dynamic client registration | endpoint present, **off per organization by default** (`400 ... disabled for this organization` unauthenticated) |
| Admin password | **no default** in this version ("set `initAdminPassword` before the first start"); a documented `admin/123` fails. Two of Casdoor's five attempts were burned finding out |
| `aud` of an access token | **follows the `resource` parameter**: sent -> `["<resource>"]`; not sent -> `["<client id>"]`. Both a vendor page and a third-party guide were right, about different requests |
| Is `resource` checked? | **No.** Any string is accepted, and nothing ties it to the application or the scopes: a token with `aud` = the sandbox URL and `scope` = `ecorp:read` was issued. Neither the audience nor the scope alone is a boundary |
| Public client | an authorization-code + PKCE exchange **without a client secret** succeeds (so `claude mcp add` needs no `--client-secret`) |
| Issuer | derived from the **request's Host header** when `origin` is unset (`https://auth.localhost:18000` for a request that said so); with `origin` set it is that value exactly |
| Token formats | `JWT` (default): **85 claims, ~3 KB**, including `password`, `passwordSalt`, `totpSecret`, `recoveryCodes` (all empty, but present), `lastSigninIp`, `createdIp`; `JWT-Empty`: 18 claims; `JWT-Standard`: 17 (16 in the live token). The gateway uses `JWT-Standard` |
| Identity claims | `owner` = the organization, `sub` = a stable user UUID, `name` = the user name (`Admin` capitalised under `JWT-Standard` for the admin: do not key on `name`) |
| Application settings | category `Agent` / type `MCP` turns custom scopes on (`resource:action`); the built-in application (no grant types listed) refused `client_credentials`; and `openid` is "Invalid scope" on an `Agent` application, which accepts only its own scopes |
| Redirect URIs | compared exactly (scheme, port, path): Claude Code needs `--callback-port` to be a fixed, registered one |
| `all-in-one` image | embedded SQLite, 117 MB: fine for a check, not what compose runs. `casbin/casdoor` (51 MB, uid 1000, built-in healthcheck) with Postgres is |

## M3. mcp 2.3.0 server-side behaviour (a throwaway prototype, then the real code)

- `MCPServer(token_verifier=..., auth=AuthSettings(issuer_url, resource_server_url, required_scopes, validate_token_resource=True))` builds the bearer middleware, the 401/403 answers, the RFC 9728
  document and a DNS-rebinding guard. A request with no token: `401`, `error="invalid_token"`, `resource_metadata="<host>/.well-known/oauth-protected-resource/<path>"`. Wrong audience: 401.
  Missing scope: `403 insufficient_scope`, `Required scope: ecorp:read`. Wrong Host: `421 Invalid Host header`.
- `get_access_token()` works inside a **stateless** tool call, and five concurrent callers of five tenants each saw their own (no contextvar leakage). An exception raised by a verifier is a **500**.
- Each `MCPServer.streamable_http_app()` carries its own auth middleware, so two servers cannot share one Starlette by merging routes; the metadata documents must sit at the host root. So the
  gateway dispatches by exact path and enters each child's lifespan itself.
- `AuthSettings(issuer_url="https://x")` keeps the string; `AuthSettings(issuer_url=AnyHttpUrl("https://x"))` appends a slash, so the metadata document named an authorization server whose identifier
  differed from the `issuer` in its own metadata and in every token. Found by reading the live document; the first test stripped slashes before comparing and hid it.
- PyJWT 2.14 itself rejects a token whose `sub` is not a string. A `scope` claim that is a number made my first scope parser raise (found by a test written to prove the verifier never raises).
- Session managers are anyio task groups: a pytest fixture cannot enter the gateway's lifespan (setup and teardown run in different tasks); each test enters it in its own body.

## M4. The live run: `make mcp-gateway-smoke` on the real stack (17 checks)

Isolated compose project (its own network and volumes; the maintainer's running stack was not touched), the repo's own Postgres with `postgres-init` (tenants `ecorp`: 5 employees, `other-co`: 1),
Casdoor on its own Postgres, Caddy with its local CA, one to three gateway replicas. Passed: a public client completes code + PKCE through the proxy; the token is RS256, `iss` = the public issuer,
`aud` = exactly the ecorp URL, `owner` = `ecorp`, `scope` = `ecorp:read`; no token -> 401 with the metadata URL; a tampered token -> 401; a token minted without `resource` (`aud` = client id) -> 401;
`ecorp` reads its five rows and not `other-co`'s; `other-co` reads only Jordan Lee; naming another tenant (and a name filter that spells it) changes nothing; two tenants at once each get their own;
an operator reads the ops server and its incident log; the operator's ops token does not open ecorp (401); an ecorp token does not open ops (401); a token the authorization server happily minted with
`aud` = ops for a non-operator tenant -> 403. `make mcp-gateway-bootstrap` run twice: nothing created the second time, client ids unchanged, passwords untouched.

## M5. Scaling and failure, measured

| Run | Result |
|---|---|
| 3 replicas, 60 calls over fresh connections | 60 of 60 ok; the audit lines per replica were about equal (no sticky routing; the first replica's higher total includes earlier checks) |
| a replica **stopped** during 30 s of steady traffic (Caddy without retries) | 619 ok, **2 failed** of 621 (the window before Caddy drops the dead upstream) |
| a replica **killed** (SIGKILL) during 30 s of steady traffic, `lb_try_duration 5s` | **615 of 615 ok** |
| logs of all six containers (~1,900 lines) searched for `eyJ`, the admin and the database password | none found |
| a gateway replica | `user=appuser`, read-only root, `cap_drop ALL`, `no-new-privileges`, `init`, no published port |

## M6. A mistake the live run caught

The first compose file wrote `${CASDOOR_DB_PASSWORD:?run make mcp-gateway-up}`. Compose interpolates the **whole file** on every command, profile or not, so every `docker compose` command
(and so `make up`) failed with "required variable ... is missing a value" for everyone who never starts this stack. `main`'s dev file has no required variable. The passwords now default to empty
(Postgres refuses to initialise without one), and `tests/core/test_compose_gateway.py` fails on a `${VAR:?` anywhere in the file.

## M7. Claude Code (documentation, https://code.claude.com/docs/en/mcp; not run here)

Remote servers: `claude mcp add --transport http <name> <url>`; OAuth through `/mcp`; Client ID Metadata Documents and dynamic registration discovered automatically; a pre-registered client with
`--client-id` and `--callback-port`; SSE deprecated. A custom CA for a Node program is conventionally `NODE_EXTRA_CA_CERTS`; **not verified with Claude Code**.

## Sources

[MCP authorization (draft)](https://modelcontextprotocol.io/specification/draft/basic/authorization) and its
[security considerations](https://modelcontextprotocol.io/specification/draft/basic/authorization/security-considerations);
[Casdoor as an MCP authorization server](https://casdoor.ai/docs/mcp-auth/overview/) and its [setup page](https://casdoor.ai/docs/mcp-auth/setup/);
[Claude Code MCP docs](https://code.claude.com/docs/en/mcp); [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707),
[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414).
