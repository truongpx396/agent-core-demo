# Data model: authlimit (spec 018)

Entities from the spec, with fields, validation and state changes. Formats are in [contracts/authlimit.md](contracts/authlimit.md); the reasons are in [research.md](research.md).

## External access token (input, never stored)

Issued by Casdoor. The service reads, and never keeps, these claims.

| Claim | Rule |
|---|---|
| header `alg` | taken from the service's allow-list (`RS256`), never from the token; `none` and HMAC are refused |
| header `kid` | must name a key in the cached key set (an unknown one triggers at most one refresh per 30 s) |
| `iss` | equals `AUTHLIMIT_ISSUER` exactly |
| `aud` | contains `AUTHLIMIT_AUDIENCE` (chat API); a client id as audience is refused. Not checked in MCP limit-only mode |
| `exp` | present and in the future, within the clock leeway (default 10 s) |
| `iat` | present (R2 in research: unverified for Casdoor); `exp − iat ≤ AUTHLIMIT_MAX_TOKEN_LIFETIME` |
| `sub` | present, text; the **principal** |
| `owner` (setting) | present, text; the **tenant** |
| `scope` | space-separated text containing `AUTHLIMIT_REQUIRED_SCOPE` (chat API) |

Any other shape (a number where text is required, a missing claim) is a refusal with a counted reason, never an exception.

## Internal identity (output, short-lived)

| Field | Rule |
|---|---|
| header `alg`, `typ` | `EdDSA`, `authlimit-identity+jwt` |
| header `kid` | names a published internal key |
| `iss` | `urn:agent-core:authlimit` |
| `aud` | `agent-core-api` |
| `sub` | the principal, copied from the verified token |
| `tenant` | the tenant, copied from the verified token |
| `scope` | the verified token's scopes |
| `jti` | random, unique per identity |
| `iat`, `nbf`, `exp` | `exp − iat` = `AUTHLIMIT_IDENTITY_TTL_SECONDS` (default 30, at most 120) |

The API accepts it only if signed by a currently published internal key, `alg` is `EdDSA`, `typ`, `iss` and `aud` match, it is inside `nbf`/`exp`, and `tenant` and `sub` are non-empty text. Otherwise 401.

## Internal key (file-backed, in `AUTHLIMIT_KEYS_DIR`)

Fields in `keyset.json`: `kid`, `publish_at`, `use_from`, `retire_at` (Unix seconds); the private half is `<kid>.pem`.

State is derived from the clock, not stored:

```
 created ──publish_at──► published (public key served, not used)
         ──use_from────► current   (signs; the newest such key signs)
         ──(a newer key becomes current)──► retiring (public key still served)
         ──retire_at───► retired   (not served; file may be deleted)
```

Rules: `use_from ≥ publish_at + overlap` for a new key; a replica refuses to start if no key is `current`; `retire_at` of the previous key is at least `use_from` of the next plus the identity lifetime plus the consumers' cache time, so no identity ever outlives its key.

## External key set (cached)

The JWKS from Casdoor: keys by `kid`, `fetched_at`, `last_refresh_attempt`. States per replica:

```
 fresh (age ≤ TTL) ──refresh fails──► stale-within-grace (age ≤ TTL + grace) ──refresh fails──► unusable (age > TTL + grace)
        ▲                                   │                                                  │ not ready, refuses all
        └────────────── refresh succeeds ───┴──────────────────────────────────────────────────┘
```

## Route class (configuration table)

`routes.json`: ordered entries `{ method, pattern, class }`, first match wins; classes `turn`, `upload`, `read`, `cancel`, `public`, `mcp`; each limited class carries `tenant {capacity, per_minute}` and `person {capacity, per_minute}`. Any path not matched is treated as `turn` and counted with reason `unlisted_route`.

## Rate bucket (Redis)

Per tenant and class, and per tenant, person and class: `millitokens` (integer, 0 to capacity × 1000), `refilled_at_ms`. Created full on first use; expires after twice the time to fill from empty. Decision rule: admit and consume one token from each if both have at least one; otherwise consume none.

## Revocation record (Redis, mirrored in memory)

`<tenant>\x00<sub>` → `revoked_at` (Unix seconds), in one sorted set. Void rule: token `iat ≤ revoked_at`. Entries older than the lifetime cap are removed. The in-memory copy carries `loaded_at`; it is "fresh" until older than the staleness ceiling.

## Replica (process)

States: `starting` → `ready` → `degraded` (stale-within-grace keys, or stale revocation copy) → `not ready` (no usable signing key, or unusable external keys, or revocation copy stale with policy `closed`). Liveness is independent of Casdoor and Redis; readiness is not.
