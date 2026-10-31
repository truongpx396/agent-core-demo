# Research: the CLI and the Telegram channel go through the front door

**Instance**: Casdoor `v4.24.0` (the digest `docker-compose.yml` pins), a throwaway project `channels-r1` on loopback `:18000` with its own database and passwords that existed for the run. Measured 2026-10-31 with `httpx` against the real HTTP API; the probes are not in the repo (they print facts about a third party and would go stale).

## R1. How can a bot act as a person it did not make sign in?

| # | Question | Measured |
|---|---|---|
| T1 | Password grant (`grant_type=password`) with `resource`? | HTTP 200, but the token's `aud` is the **client id**, `resource` ignored; a refresh with `resource` is then refused ("expected: [], got: [...]"). authlimit requires the API's audience, so the password grant cannot reach the API. |
| T1 | Does the grant need the client secret? | No: the same token without `client_secret`. The secret protects nothing at the token endpoint. |
| T2 | Refusals | wrong password: "invalid username or password ... you have 4 remaining chances" (five lock a person); missing: "The user: <org>/<name> doesn't exist"; disabled: "The user is forbidden to sign in, please contact the administrator". All HTTP 200/400 with Casdoor's own words. |
| T3 | **What do an application's client id and secret (HTTP Basic) allow?** | `add-user` in the application's organization: **yes**. `update-user` on a *non-bot* person (reset their password): **yes**. `delete-user`: **yes**. `get-users`, `get-application`: yes. `add-user` in another organization: no. Wrong secret, or the id alone: no. **They are an administrator of the whole organization.** |
| T4 | Self sign-up (`/api/signup`) | Refused unless the application enables it; enabled, it asks for a phone number. Open sign-up would let anyone create a tenant member, so it is not an option. |
| T5 | Token exchange (RFC 8693) | Works for an application that lists the grant, but `requested_subject` is **ignored** (the token is for the subject token's own person). No impersonation path; no application enables the grant. |
| T6 | Can a Telegram person's password sign in at *another* application of the organization (the web page's)? | **Yes** (HTTP 200 with a code). Any application of the organization accepts any of its people's passwords. |
| C | Headless authorization-code flow with the repo's real parameter names (`code_challenge`, `resource` on the authorize request AND the exchange) | `aud` = the API's URL, `owner` = the organization, `scope` = `api:chat`, lifetime 604800 s, a refresh token; a refresh with the same `resource` works. |
| C | A disabled person | sign-in refused ("forbidden"); their refresh token is revoked at once ("refresh token is invalid or revoked"). |
| D | Discovery | lists `password`, `client_credentials`, `refresh_token`, token-exchange and **device code** (`/api/device-auth`). The device flow was not used: completing it needs the verification page, which a test cannot drive; the loopback flow is the one `scripts/authlimit_smoke.py` already proves. |

### Consequences (each became a decision)

1. **The bot signs in with the authorization-code flow, headless** (T1, C), by posting a person's credentials where the login page would.
2. **The bot does not store per-person secrets**: the password is `HMAC(master secret, tenant, id)`. A leak of that one secret exposes the Telegram people only (T3 makes any alternative that stores an application credential a tenant takeover).
3. **By default the bot cannot create a person** (T3): an operator does (`make telegram-person`). `TELEGRAM_AUTO_PROVISION` is the opt-in, with the cost written where it is set. This overturned spec A3 and FR-005 as first drafted.
4. **FR-005 cannot hold as drafted** (T6): a Telegram person's password also signs in at the web page and at an MCP client's sign-in. Only Telegram people are exposed, never anyone else; disclosed in GRAPH_PATTERNS.md 91 and the README.
5. **Disabling is the durable revocation** (C): it ends refresh at once. A token already issued lives until it expires (seven days) or `make authlimit-revoke` voids it; `make telegram-person ACTION=disable` prints that line.

## Verified live (against the real Casdoor above)

`scripts/mcp_gateway_bootstrap.py` (registers the CLI callback), then in one run: an unregistered user is refused (`unknown_person`); `scripts/telegram_person.py add` creates the person; `TelegramPeople.sign_in` returns a token with `aud` = the API, `owner` = the tenant, `name` = `telegram-<id>`; a fresh process signs in as the same person (`sub` equal); two users are two people; `disable` -> sign-in refused (`forbidden`) and the old refresh token answers 400; `enable` -> signs in again; the CLI's real loopback listener caught a redirect, exchanged the code, wrote a mode-600 session, reused it with no browser on the second run, and `reauthenticate` refreshed it.

## Verified hermetically

The door client against a mock transport (22 cases) and against the **real FastAPI app** in process with a fake Redis and a worker that answers (7): the request the client builds is the one the routes accept, the SSE the routes stream is what the client parses, the `unattended`/`require_approval` flags reach the job, a replay key is one job, a `telegram:<person>:<chat>` id is claimable by that person and no one else, and a cancel for another person's conversation sets no flag. 16 deliberate defects in the new code (flags dropped, the thread rule loosened, the replay window shortened, a forbidden person re-created, auto-provision on by default, the lock removed, a world-readable session file, the state check removed, ...) each fail a test (found two that did not, and fixed the tests).

## NOT verified (stated, not hidden)

- **Through the real door.** Nothing here ran Caddy + authlimit + the API + a worker together. That the door accepts a Telegram person's token follows from it being the same kind of token the web page's sign-in produces (claims measured above), but is **not observed**. SC-002 (door stopped -> a visible failure and nothing queued) is tested against a mock; SC-003 (rate limit with a wait time, cancels exempt), SC-006 (revocation within 5 s) and SC-009 (added latency) need the full stack and were **not measured**. `make authlimit-smoke ARGS="--upstream api"` is the existing end-to-end tier to extend.
- **A real Telegram bot**: no token was available; the Telegram HTTP API is faked in the tests, as before.
- **The auto-provision path against real Casdoor** (T3 shows the credentials can create a person; the bot's `_provision` was tested against the fake only).
- **A real browser** for the CLI sign-in (the redirect was followed by a script; the authorize URL's parameters are the web page's).
