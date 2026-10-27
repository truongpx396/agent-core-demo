# authlimit

The edge authentication and rate-limit service (`specs/018-authlimit-edge-auth/`). A small Go program that Caddy asks about **every request** to the chat API and the MCP routes, through `forward_auth`, before the request goes any further.

```
client ─► Cloudflare (later) ─► Caddy ──(1) GET /v1/check + the caller's Authorization──► authlimit ─┬─► Casdoor's signing keys (cached)
                                  │                                                                 └─► Redis (buckets, revocation)
                                  │ ◄─(2) 200 + X-Internal-Identity        or 401 / 403 / 429 / 503, sent straight to the client
                                  └──(3) the request, with that header and WITHOUT Authorization ──► the API
```

What it does for each request, in this order (`internal/server/server.go`):

1. **Classifies the route** from `routes.json`: public (not checked), verified and limited (a chat turn, an upload), verified and never limited (cancel, reads), or MCP (limit-only). A route nobody listed is verified and limited like a turn.
2. **Verifies the Casdoor access token offline** (`internal/extjwt`): RS256 only (taken from an allow-list, never from the token), issuer, audience, expiry, an issue time, a scope, a lifetime cap. It never panics; a refusal is a counted reason, never the token.
3. **Refuses revoked people** (`internal/revoke`) before touching any bucket.
4. **Charges the tenant's and the person's token buckets** in Redis in one atomic call (`internal/limiter`).
5. **Mints a 30-second Ed25519-signed identity** (`internal/identity`) that only the API trusts.

On the MCP routes it runs **limit-only**: the MCP gateway verifies the token itself, so this service only identifies the caller to meter and revoke them, and passes everything else through for the gateway to answer.

## Failure policy

| Failure | Policy |
|---|---|
| No usable Casdoor keys (never fetched, or older than the TTL plus the grace) | refuse everything, 503, report not ready: **fail closed** |
| Casdoor unreachable, keys inside the grace (6 h) | serve from the last keys, count it, alert |
| Revocation copy older than 30 s | refuse (503) by default, or serve: configurable |
| Redis down or slow for the limiter | **serve**, count it, alert: a protection must not become an outage |
| No signing key at start | refuse to start |

## Commands

```
authlimit serve                                   # the service (the default)
authlimit healthcheck [--ready]                   # exit 0 if this replica answers; the container healthcheck (the image has no shell)
authlimit keys init|rotate|list --dir D           # the internal signing keys; never prints key material
authlimit revoke --tenant T --sub S               # void every token issued to S up to now, on every replica
authlimit revoke --list
```

`make authlimit-test` runs the tests; `make authlimit-up` / `-smoke` / `-keys` / `-revoke` / `-latency` / `-mutants` are the stack and its checks (see the repository README). Settings are environment variables, validated at start with an error that names the variable and never its value; the list with defaults is in `specs/018-authlimit-edge-auth/contracts/authlimit.md` section E.

## Layout

```
cmd/authlimit/       serve, healthcheck, keys, revoke   (cmd_test.go runs the real wiring end to end)
cmd/echo/            the stub upstream of the smoke check (a test fixture)
internal/config      environment -> validated Config
internal/extjwt      the external token verifier and the Casdoor key cache (with the stale grace)
internal/identity    the key directory, rotation, minting the internal identity
internal/limiter     routes.json and the Redis token-bucket script (bucket.lua)
internal/revoke      the revocation list and its local copy
internal/server      /v1/check, /healthz, /readyz, /internal/jwks.json
internal/errenv      the {code, message, details} error envelope
internal/telemetry   OTLP metrics and JSON logs
internal/reason      the one vocabulary of refusal reasons (metric labels, logs, tests)
routes.json          the route-class table          errorcodes.json   the error codes (the Python side checks against it)
```

## Rules this module keeps

- **It imports nothing from the rest of the repository, and the rest of the repository imports nothing from it** (`tests/core/test_authlimit_boundary.py`; constitution 1.4.0, "Non-Python services"). The only coupling is the HTTP contract, `routes.json`, `errorcodes.json` and the shared token vectors in `testdata/auth-vectors/`, which the tests of both languages read.
- **No private key is ever committed or put in an image.** The signing keys are mounted from a directory outside the repository (`make authlimit-keys`). The vectors were signed with a throwaway key that was discarded.
- **No token, key or claim value other than a tenant or person id reaches a log, a metric label or an error body.** Metric names are literals in `internal/telemetry/metrics.go`, which `tests/core/test_alert_rules.py` reads, so an alert on a metric nothing records fails the build.
- The tests are hermetic (an in-memory Redis, a fake authorization server, an injected clock). The real behaviour is checked by `make authlimit-smoke`, and the claim that the tests would catch a defect by `make authlimit-mutants`.
