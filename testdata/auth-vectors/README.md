# Shared token vectors (spec 018)

One file of tokens, read by two verifiers in two languages, so they cannot quietly disagree about what a valid token is:

- the Go service, `services/authlimit/internal/extjwt`;
- the MCP gateway's Python verifier, `app/mcp/gateway_auth.py` (`tests/mcp/test_gateway_vectors.py`, which also freezes the clock at `now`).

`vectors.json` is the cases, `jwks.json` is the public key set that verifies them. Both are produced by `scripts/gen_auth_vectors.py`.

## No secret is kept here

Every token was signed once with a throwaway RSA key generated in memory, and the key was discarded. Only the public half (`jwks.json`)
and the finished tokens are committed. Nothing in this directory can mint a new token. Re-running the script makes a **new** set (new key),
so commit its output only when a vector is added or changed.

## The clock is part of the file

`now` is the instant a verifier under test must use as its clock. Every `exp`, `iat` and `nbf` is relative to it, so no vector ever
expires. `config` holds the settings the vectors were designed against (issuer, audience, scope, claims, leeway, lifetime cap); a verifier
under test is configured with exactly those.

## Three profiles

The verifiers do different amounts of checking, so each vector says what each profile must conclude:

| Profile | Who | Checks |
|---|---|---|
| `chat` | the Go service on the chat API | signature, algorithm, issuer, the API's audience, expiry, an issue time, a lifetime cap, a scope, tenant and principal are text |
| `identify` | the Go service in limit-only mode on the MCP routes | signature, algorithm, issuer, expiry, tenant and principal are text. **Not** audience, scope, issue time or lifetime cap |
| `mcp` | the MCP gateway's own verifier | signature, algorithm, issuer, its own audience (`config.mcp_audience`), expiry, a subject |

Each `expect` entry is `{"result": "valid", "tenant", "principal"}` or `{"result": "refused", "reason"}`.

- For `chat` and `identify`, `reason` is exact: it is the Go service's vocabulary (`internal/reason`).
- For `mcp`, only `result` is compared. The reason is informational: the MCP gateway counts its own words.

## The rule between `identify` and `mcp`

**The limit-only profile never refuses a token the MCP gateway accepts.** If it did, that token would pass through to the gateway without
being counted against any bucket: a way around the limiter. `scripts/gen_auth_vectors.py` refuses to write a file that breaks this, and
the tests of both verifiers re-check it. (Clock tolerance is the one thing the vectors cannot show: see `specs/018-authlimit-edge-auth/contracts/authlimit.md`, A.1.) (The reverse is fine: limit-only mode may accept a token the gateway then refuses.)

In particular a token with no issue time is valid for `identify`. The service then treats its issue time as 0, so any revocation of that
person voids it: the safe direction.

## What the cases cover

Valid tokens (including an audience list, the exact lifetime cap, expiry inside the leeway, and tokens meant for an MCP endpoint); expiry,
`nbf` and `iat` in the future; missing `exp`, `sub`, `iat`; a lifetime over the cap; tenant and principal that are not text, empty or
missing; wrong issuer, wrong audience, the client id as audience (what the authorization server mints when no `resource` was sent);
missing, non-text, list and prefix-only scopes; `alg: none`, HMAC signed with the public key (algorithm confusion), an algorithm outside the
allow-list, the service's own internal identity presented as an access token; an unknown `kid`, no `kid`, a foreign key claiming our `kid`,
a tampered payload; and input that is not a token at all.
