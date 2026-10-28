# Quickstart: validating authlimit locally (spec 018)

Runnable checks that prove the feature end to end on one machine: Casdoor, Caddy, Redis, two replicas of the service. No Cloudflare. The make targets are the ones PR 1 and PR 2 add; until they exist this is the plan for them.

## Prerequisites

- Docker, and the repository's base stack: `make up` (Postgres and Redis).
- The Casdoor and Caddy images already used by the `gateway` profile (spec 017).
- `source .venv/bin/activate` for the Python smoke; Go 1.26 or later to run `make authlimit-test` outside Docker.

## 1. Build and unit-test the service (PR 1, no services needed)

```bash
make authlimit-test        # go vet + go test -race ./... ; runs the shared token vectors
make authlimit-build       # builds the image
```

Expected: all tests pass; the vectors file reports every vector with its expected outcome.

## 2. Bring up the stack (PR 1)

```bash
make authlimit-keys        # first internal signing key, in a directory outside the repo (once)
make authlimit-up          # Casdoor + its Postgres, authlimit x2, the echo upstream, Caddy
make mcp-gateway-bootstrap # Casdoor organizations, applications (now incl. the API application), demo users
make mcp-gateway-ca        # trust Caddy's local CA
```

Expected: `docker compose ps` shows two `authlimit` containers healthy; `https://api.localhost:8443/` answers through Caddy.

## 3. Scenarios (the smoke runs all of them; each can be run by hand)

| # | Do | Expect |
|---|---|---|
| Q1 | Sign in as an `ecorp` user (authorization code + PKCE), call the echo through `https://api.localhost:8443/chat/sessions` with the token | 200; the echo shows `X-Internal-Identity`, tenant `ecorp`, the user's id, and **no** `Authorization` header |
| Q2 | Same call with no token, a tampered token, an expired token, a token for the MCP audience | 401 with the envelope and `WWW-Authenticate`; the echo is not called |
| Q3 | Send forged `X-Tenant-Id`, `X-Principal-Id`, `X-Internal-Identity` (once, twice, lower case) with a valid token | the echo shows only the real identity |
| Q4 | `POST /chat/stream/queued` repeatedly as `ecorp` until limited; meanwhile as `other-co`, and `POST /chat/cancel` as `ecorp` | `ecorp` gets 429 with `Retry-After`; `other-co` and cancel are served |
| Q5 | Set `AUTHLIMIT_LIMIT_MODE=observe`, repeat Q4 | no 429; the would-limit counter rises |
| Q6 | `docker kill` one `authlimit` replica under steady traffic | no failures beyond requests in flight on it; traffic continues on the other |
| Q7 | Stop `casdoor` for less than the grace, then beyond it (set the grace to 30 s for the check) | inside: existing tokens work, stale-keys metric rises, new sign-ins fail; beyond: 503 `auth_unavailable`; after restart, service resumes within one refresh |
| Q8 | `make authlimit-keys ARGS="rotate --overlap 20s"` under traffic | no failed request; the new `kid` appears on `/internal/jwks.json` |
| Q9 | `make authlimit-revoke TENANT=ecorp SUB=<user id>` | the user's old token is refused on both replicas within 5 s, on a chat route and on `/ecorp/mcp`; a fresh sign-in works |
| Q10 | Call `/ecorp/mcp` with a valid token until limited; call it with no token | 429 after the limit; no token → the MCP gateway's own 401 with its metadata |
| Q11 | Search all service logs for the access token, the refresh token, the private key | no match |

The smoke check's stages: `identity` (Q1), `refusals` (Q2), `spoofing` (Q3), `limits` (Q4), `mcp` (Q10), `revoke` (Q9), `logs` (Q11); with `ARGS=--all` also `observe` (Q5), `replica` (Q6), `rotate` (Q8), `outage` (Q7). Run one with `make authlimit-smoke ARGS="--stages revoke"`. The destructive ones restart authlimit (and for `outage` stop Casdoor) and put everything back.

Measured on a development stack (research R21): all of the above pass; the check adds about half a millisecond; revocation reaches every replica in about 3 s; a killed replica fails no request.

## 4. The API behind the front door (PR 2)

```bash
AUTH_MODE=identity make authlimit-up-app   # the real api behind the same Caddy site
make authlimit-smoke                       # Q1-Q11 again against the real API, plus: two tenants see only their own rows
```

Expected: the smoke passes; in the web UI at `https://api.localhost:8443/` the tenant selector is gone, a sign-in redirect happens, and a chat turn runs as the signed-in person.

## 5. Header mode still works for direct runs

```bash
AUTH_MODE=headers make serve               # the pre-existing way to run the API alone
make test                                  # fixtures select header mode; suite unchanged
```
