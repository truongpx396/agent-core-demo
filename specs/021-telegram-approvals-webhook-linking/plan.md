# Implementation Plan: Telegram approvals, parallel chats, a webhook, growing replies and account linking

**Spec**: [spec.md](spec.md) · **Research**: [research.md](research.md) · **Tasks**: [tasks.md](tasks.md) · **Branch**: `feat/telegram-approvals-linking-webhook` · **Base**: `feat/channels-behind-the-front-door` (#226)

## Design in six decisions

1. **`Bot` is the flow; the rest are small modules with one job.** `telegram_api.py` (every Bot API call, best effort), `telegram_approvals.py` (state and words), `telegram_reply.py` (the growing message), `telegram_inbox.py` (the Redis list and the dispatcher), `telegram_server.py` (webhook and link pages), `telegram_link.py` (state, vault, refresh).
2. **Transports only push.** Polling and the webhook put the update on the inbox and acknowledge; the dispatcher answers. This is what makes parallel chats, `/cancel` and a webhook possible at once (pattern 93).
3. **The approval is a record in Redis with a claim.** The button carries a token; the record names the Telegram user, chat and conversation; the first tap claims it (pattern 92).
4. **A stale pause is cleared by the next message, not by a timer or a late tap.** The retry waits for the cancel job and uses a new replay key.
5. **Linking keeps a sealed refresh token, never an identity.** Each turn refreshes it; the door verifies the result (pattern 94). The confirmation page exists for one named attack.
6. **The bot's listener is its own.** Telegram and a returning browser carry no front-door token, so these routes are not behind the door; each is authenticated by what it carries (the secret, the state).

## Constitution Check

| Principle | Touched | How it holds |
|---|---|---|
| **I. Fail-closed tenant isolation** (NN) | Yes (linking) | The tenant is read from the verified-at-the-door token (`owner`) on every turn; the callback accepts no tenant from its query; another tenant's account is refused; the stored `sub` is never used as identity; keys carry the tenant. An unlinked, revoked or unreachable sign-in is a refusal, never a default tenant. |
| **II. Mandatory approval** (NN) | Yes, the point | Only `POST /chat/resume` after the owner's tap approves; no timeout or default approves. Owner-only (record names the user; the API checks the signed-in person too), one claim, expiry never acts, a late tap cancels nothing. `unattended` still only tightens. Tests are mutation-checked (owner check, claim, attended flag). |
| **IV. Exactly-once side effects** (NN) | Indirectly | A double tap and a redelivered callback cannot resume twice (the claim); a redelivered update is one turn (`replay_key`); the tool-level idempotency guard is untouched. |
| V. Bounded, observable failure | Yes | `channel_telegram_update_total`, `_approval_total`, `_link_total`, `_webhook_total`; alerts `TelegramUpdatesDropped` and `TelegramWebhookProbed`; every wait has an end (`take` timeout, cancel `wait`, state TTLs); no token or secret is logged (tested for the bot token). |
| VII. Real backends where they matter | Partly | Not met for the Telegram and Casdoor edges, stated in research.md. |
| VIII. Why-first docs, honest gaps | Yes | Patterns 92 to 94 each end with their gaps; the README lists what was not verified. |

## Structure

```text
app/channels/{telegram,telegram_api,telegram_approvals,telegram_inbox,telegram_link,telegram_reply,telegram_server}.py
app/channels/door.py            cancel_turn(wait=...), "not_linked" words
app/core/{config,metrics}.py    13 settings, 4 counters
scripts/mcp_gateway_bootstrap.py  --telegram-link-url
observability/prometheus/alerts.yml  2 rules
tests/channels/{telegram_fakes,test_telegram_*}.py
```

## Mutation check (run, not assumed)
Each of these made at least one test fail when removed, then was restored: owner check · claim · `unattended=not attended` · awaited stale cancel · webhook secret · single-use state (`GETDEL`) · tenant check · serialized refresh · `/cancel` bypass · offset-after-push.
