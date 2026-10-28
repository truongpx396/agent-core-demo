# Contract: authlimit (spec 018)

Everything another component depends on. Part A is Caddy's, B the API's, C the operator's, D the Redis layout, E the settings. Behaviour of `forward_auth` quoted here was measured on Caddy 2.11.7 (research R1).

## A. Front door to service

Caddy calls the service once per request to a non-public route:

```
GET /v1/check          (the `uri` of forward_auth)
Authorization: Bearer <external token>     forwarded unchanged
X-Forwarded-Method: <original method>
X-Forwarded-Uri:    <original path and query>
X-Forwarded-For / -Host / -Proto
(no body)
```

The route class comes from `X-Forwarded-Method` and the path of `X-Forwarded-Uri` against `routes.json`.

| Class | Condition | Status | Answer |
|---|---|---|---|
| `public` | any | 200 | none (Caddy should not call; harmless if it does) |
| `turn`, `upload` | valid token, both buckets admit | 200 | header `X-Internal-Identity: <jwt>` |
| `read`, `cancel` | valid token | 200 | header `X-Internal-Identity: <jwt>` (never limited) |
| any verified class | no token | 401 | `WWW-Authenticate: Bearer realm="agent-core-api"`; code `unauthenticated` |
| any verified class | invalid token (any reason) or revoked | 401 | `WWW-Authenticate: Bearer realm="agent-core-api", error="invalid_token"`; code `unauthenticated` |
| any verified class | valid token without the scope | 403 | code `forbidden`, `details.required_scope` |
| `turn`, `upload` | a bucket empty, mode `enforce` | 429 | `Retry-After: <seconds>`; code `rate_limited`, `details.retry_after_seconds` |
| `turn`, `upload` | a bucket empty, mode `observe` | 200 | as admitted; `would_limit` counted |
| any verified class | keys or revocation copy unusable (policy closed), or no current signing key | 503 | `Retry-After: 30`; code `auth_unavailable` (reasons `keys_unavailable`, `revocation_unavailable`, `signing_unavailable`) |
| `mcp` | valid token, buckets admit | 200 | no identity header |
| `mcp` | missing or invalid token | 200 | no identity header, nothing limited (the MCP gateway answers 401 with its metadata) |
| `mcp` | revoked | 401 | `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<template>"`; code `unauthenticated` |
| `mcp` | bucket empty, mode `enforce` | 429 | as above |

Default buckets (`routes.json`; capacity and refill per minute): a chat turn or upload, tenant 10 / 30, person 5 / 10; an MCP route, tenant 60 / 120, person 30 / 60 (an MCP tool is a cheap read and one client session makes several requests).

### A.1 Agreement with the MCP gateway's verifier

In the `mcp` class a token the service cannot identify is passed on to the gateway uncounted (the gateway answers 401 with its metadata), so the service must never be **stricter** than the gateway or a token would be served without being charged to any bucket. Both verifiers run the same vectors (`testdata/auth-vectors/`, profiles `identify` and `mcp`; `tests/mcp/test_gateway_vectors.py` for the gateway) and agreed on all 37 on the first run: T066 found no case to fix, and the gateway's verifier was not changed. Two differences are known and deliberate:

- **The gateway checks less than the chat profile**: no scope, no issue time, no lifetime cap (it checks its own audience instead). The limit-only profile checks the same as the gateway, so this is the same set seen from both ends.
- **Clock tolerance is not in the vectors.** The gateway's is a constant 5 seconds; the service's is `AUTHLIMIT_CLOCK_LEEWAY` (10 seconds by default). While the service's is at least the gateway's, nothing the gateway accepts at the edge of expiry escapes counting. Lowering the setting below 5 seconds opens a window of the difference in which a just-expired token is served uncounted; a test pins the default relationship, not an operator's override.

Error body: `{"code": "...", "message": "...", "details": {...}}`, `Content-Type: application/json`. Messages never contain a token, claim or exception text. Codes are listed in `services/authlimit/errorcodes.json`.

Other endpoints (not routed publicly by Caddy): `GET /healthz` (liveness, always 200 while the process runs), `GET /readyz` (200 or 503; ready means a usable signing key, usable external keys within the grace, and, under policy `closed`, a revocation copy within its ceiling), `GET /internal/jwks.json` (public internal keys, OKP/Ed25519 JWKS).

Caddy's side, as built (the exact text, with the reasons, is `deploy/caddy/Caddyfile.gateway`; behaviour measured on Caddy 2.11.7, research R1 and R21):

```
(authlimit_pool) {                        # shared by every forward_auth below
	dynamic a { name authlimit  port 8080  refresh 5s }
	health_uri /readyz  health_interval 10s
	lb_try_duration 5s  lb_try_interval 250ms
	fail_duration 20s  max_fails 1            # one failed connect skips a dead replica for 20 s ...
	transport http { dial_timeout 1s }        # ... and a dead address costs 1 s, not the default 3 s, per request
}

https://api.localhost:8443 {              # the chat API
	@public_path {
		path / /health /health/ready /static/* /billing/webhooks/* /.well-known/*
		not expression `{http.request.uri}.matches(r"%|\\|\.\.|//")`   # only PLAIN URLs are public here (R21 finding 1)
	}
	handle @public_path { reverse_proxy {$AUTHLIMIT_UPSTREAM} { flush_interval -1 } }
	@preflight method OPTIONS
	handle @preflight   { reverse_proxy {$AUTHLIMIT_UPSTREAM} { flush_interval -1 } }
	handle {
		forward_auth { import authlimit_pool  uri /v1/check  copy_headers X-Internal-Identity }
		request_header -Authorization         # after the check, before the API
		request_header -X-Tenant-Id           # the pre-018 way to name the caller never reaches the API through the door
		request_header -X-Principal-Id
		reverse_proxy {$AUTHLIMIT_UPSTREAM} { flush_interval -1 }      # authlimit-echo:8000 now; api:8000 in PR 2
	}
}

https://mcp.localhost:8443 {              # the MCP routes live on the MCP host, where tokens' audience is already pinned
	@mcp path /ecorp/mcp /ops/mcp
	handle @mcp {
		forward_auth { import authlimit_pool  uri /v1/check }          # limit-only: no copy_headers, Authorization is KEPT
		reverse_proxy { import mcp_gateway_pool }
	}
	@mcp_metadata path /.well-known/oauth-protected-resource/ecorp/mcp /.well-known/oauth-protected-resource/ops/mcp
	handle @mcp_metadata { reverse_proxy { import mcp_gateway_pool } }   # discovery needs no token
}
```

## B. Service to API (internal identity)

Header `X-Internal-Identity: <compact JWS>`. Format and validation rules: [data-model.md](../data-model.md#internal-identity-output-short-lived). The API:

- in identity mode requires it on every non-public route and builds `SecurityCtx` from `tenant` and `sub` only; a missing or invalid one is `401` with the envelope code `unauthenticated`; every other identity header is ignored;
- fetches public keys from `AUTHLIMIT_KEYS_URL` (`/internal/jwks.json` on the service), caches them for `AUTHLIMIT_KEYS_CACHE_SECONDS` (60 s), refreshes on an unknown `kid` at most once per 10 s, and refuses to start without the URL (the message names the variable, never its value);
- if a refresh fails keeps trusting the last keys for `AUTHLIMIT_KEYS_STALE_GRACE_SECONDS` more (600 s; counted in `agent_api_identity_key_refresh_total{result="error"}`), then **fails closed**: every request is answered `503` with code `auth_unavailable` and `Retry-After: 30`, not `401`, because the caller did nothing wrong (`agent_api_identity_total{outcome="keys_unavailable"}`). The grace exists so one dead `authlimit` replica behind a round-robin name does not turn one failed fetch into an outage; its cost is that a key withdrawn for compromise stays trusted that long when the service cannot be reached;
- in header mode (explicit opt-in) reads `X-Tenant-Id` and `X-Principal-Id` as today and announces it (log line at start, gauge `agent_api_auth_mode`).

## C. Operator commands

```
authlimit serve                                   # the service (default command)
authlimit healthcheck                             # exit 0 if /healthz answers; used by the container healthcheck
authlimit keys init   --dir D                     # first signing key, mode 700 directory, never overwrites
authlimit keys rotate --dir D [--overlap 10m]     # add a key, schedule use and retirement
authlimit keys list   --dir D                     # kid, state, times (no key material)
authlimit revoke --tenant T --sub S               # tokens issued to S before now are void on every replica within 5 s
authlimit revoke --list                           # current revocations
```

Make targets (PR 1): `authlimit-test`, `authlimit-build`, `authlimit-keys` (`ARGS=init|rotate|list`), `authlimit-up`, `authlimit-down`, `authlimit-smoke` (`ARGS=--all` adds the stages that stop things), `authlimit-revoke TENANT= SUB=`, `authlimit-latency`, `authlimit-mutants`.

## D. Redis layout (prefix `authlimit:v1:`)

| Key | Type | Content |
|---|---|---|
| `b:{<tenant>}:t:<class>` | hash | `mt` (millitokens), `ts` (ms) |
| `b:{<tenant>}:p:<sub>:<class>` | hash | same |
| `revoked` | sorted set | member `<tenant>\0<sub>`, score `revoked_at` |

Nothing else is written; no token, claim value other than tenant and sub, or key is ever stored.

## E. Settings

Service (environment; invalid or missing required values stop the start):

| Variable | Default | Meaning |
|---|---|---|
| `AUTHLIMIT_ISSUER` | required | Casdoor's issuer; `https` unless loopback |
| `AUTHLIMIT_JWKS_URL` | required | key fetch address; `http` allowed for loopback or an in-network (dotless) name, as in spec 017 |
| `AUTHLIMIT_AUDIENCE` | required | the API's resource identifier |
| `AUTHLIMIT_REQUIRED_SCOPE` | `api:chat` | scope the chat API needs |
| `AUTHLIMIT_TENANT_CLAIM` / `_PRINCIPAL_CLAIM` | `owner` / `sub` | claims naming them |
| `AUTHLIMIT_MAX_TOKEN_LIFETIME` | `168h` | longest `exp − iat` accepted |
| `AUTHLIMIT_CLOCK_LEEWAY` | `10s` | skew allowed |
| `AUTHLIMIT_JWKS_CACHE_SECONDS` / `_MIN_REFRESH_SECONDS` / `_FETCH_TIMEOUT_SECONDS` | `300` / `30` / `5` | key cache |
| `AUTHLIMIT_JWKS_STALE_GRACE_SECONDS` | `21600` | serve stale keys this long |
| `AUTHLIMIT_KEYS_DIR` | required | internal keyset directory |
| `AUTHLIMIT_KEYS_ALLOW_LOOSE_PERMS` | `false` | accept key files readable by group or others (a development bind mount whose ownership Docker maps); never in production |
| `AUTHLIMIT_KEY_OVERLAP` | `10m` | used by `keys rotate` when `--overlap` is not given |
| `AUTHLIMIT_KEY_PREFIX` | `authlimit:v1:` | prefix of every Redis key (must end in `:`) |
| `AUTHLIMIT_LISTEN` | `:8080` | listen address (also read by `healthcheck`) |
| `AUTHLIMIT_LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error` |
| `AUTHLIMIT_PRINCIPAL_CLAIM` / `AUTHLIMIT_TENANT_CLAIM` | `sub` / `owner` | (also listed above) |
| `AUTHLIMIT_IDENTITY_TTL_SECONDS` | `30` (max 120) | internal identity lifetime |

The API's side of the same seam (settings of the Python app, `app/core/config.py`; not read by the service):

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_MODE` | `identity` | `identity` trusts only the signed internal identity; `headers` trusts `X-Tenant-Id` and `X-Principal-Id` (tests, and the API run alone). Anything else is refused at import |
| `AUTHLIMIT_KEYS_URL` | none | where the API fetches the service's public keys; required in identity mode (the API does not start without it); `https`, or `http` for loopback or an in-network name without a dot |
| `AUTHLIMIT_KEYS_CACHE_SECONDS` | `60` (10 to 3600) | how long a fetched key set is trusted |
| `AUTHLIMIT_KEYS_STALE_GRACE_SECONDS` | `600` (0 to 3600) | how much longer the last key set is trusted when a refresh fails; `0` means not at all |
| `AUTHLIMIT_REDIS_URL` | required | the existing Redis |
| `AUTHLIMIT_REDIS_TIMEOUT_MS` | `100` | ceiling on a limiter call |
| `AUTHLIMIT_LIMIT_MODE` | `enforce` | `enforce` or `observe` |
| `AUTHLIMIT_REVOCATION_POLL_MS` | `1000` | list refresh |
| `AUTHLIMIT_REVOCATION_MAX_STALENESS_SECONDS` | `30` | then policy applies |
| `AUTHLIMIT_REVOCATION_UNAVAILABLE` | `closed` | `closed` or `open` |
| `AUTHLIMIT_ROUTES_FILE` | embedded table | override |
| `AUTHLIMIT_MCP_METADATA_URL_TEMPLATE` | required if `mcp` routes exist | for the revoked-person 401 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset = no export | metrics |

API (`Settings`, with `.env.example` entries, PR 2): `AUTH_MODE` (`identity` default, or `headers`), `AUTHLIMIT_KEYS_URL` (required in identity mode), `AUTHLIMIT_KEYS_CACHE_SECONDS` (`60`).

Casdoor bootstrap (PR 1): `--token-hours` / `TOKEN_HOURS` (default `168`) for every application it creates.
