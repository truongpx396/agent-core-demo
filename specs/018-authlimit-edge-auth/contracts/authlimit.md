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
| any verified class | keys or revocation copy unusable (policy closed) | 503 | `Retry-After: 30`; code `auth_unavailable` |
| `mcp` | valid token, buckets admit | 200 | no identity header |
| `mcp` | missing or invalid token | 200 | no identity header, nothing limited (the MCP gateway answers 401 with its metadata) |
| `mcp` | revoked | 401 | `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<template>"`; code `unauthenticated` |
| `mcp` | bucket empty, mode `enforce` | 429 | as above |

Error body: `{"code": "...", "message": "...", "details": {...}}`, `Content-Type: application/json`. Messages never contain a token, claim or exception text. Codes are listed in `services/authlimit/errorcodes.json`.

Other endpoints (not routed publicly by Caddy): `GET /healthz` (liveness, always 200 while the process runs), `GET /readyz` (200 or 503; ready means a usable signing key, usable external keys within the grace, and, under policy `closed`, a revocation copy within its ceiling), `GET /internal/jwks.json` (public internal keys, OKP/Ed25519 JWKS).

Caddy's side (illustrative, final text in `Caddyfile.gateway`):

```
https://api.localhost:8443 {
	tls internal
	@public path / /health /health/ready /static/* /billing/webhooks/*
	handle @public { reverse_proxy api:8000 }
	@mcp path /ecorp/mcp /ops/mcp
	handle @mcp {
		forward_auth { dynamic a { name authlimit  port 8080  refresh 5s }  uri /v1/check  lb_try_duration 5s  lb_try_interval 250ms }
		reverse_proxy mcp-gateway:8000       # the Authorization header is NOT removed here
	}
	handle {
		forward_auth { dynamic a { name authlimit  port 8080  refresh 5s }  uri /v1/check  copy_headers X-Internal-Identity  lb_try_duration 5s  lb_try_interval 250ms }
		request_header -Authorization        # after the check, before the API
		reverse_proxy api:8000 { flush_interval -1 }
	}
}
```

## B. Service to API (internal identity)

Header `X-Internal-Identity: <compact JWS>`. Format and validation rules: [data-model.md](../data-model.md#internal-identity-output-short-lived). The API:

- in identity mode requires it on every non-public route and builds `SecurityCtx` from `tenant` and `sub` only; a missing or invalid one is `401` with the envelope code `unauthenticated`; every other identity header is ignored;
- fetches public keys from `AUTHLIMIT_KEYS_URL` (`/internal/jwks.json` on the service), caches them for 60 s, refreshes on an unknown `kid` at most once per 10 s, and refuses to start without the URL;
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

Make targets (PR 1): `authlimit-test`, `authlimit-build`, `authlimit-keys`, `authlimit-up`, `authlimit-down`, `authlimit-smoke`, `authlimit-revoke TENANT= SUB=`.

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
| `AUTHLIMIT_IDENTITY_TTL_SECONDS` | `30` (max 120) | internal identity lifetime |
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
