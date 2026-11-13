# Tasks: Telegram approvals, parallel chats, a webhook, growing replies and account linking

`[x]` done and verified as the line says (hermetic tests); `[ ]` not done.

## Phase 1: Foundations
- [x] T001 Settings (13) in `Settings` and `.env.example`; metrics (4); `cryptography` as a direct requirement (already in the lock file).
- [x] T002 `telegram_api.py` and its tests (request shapes, best effort, token never logged).
- [x] T003 `telegram_inbox.py`: inbox, processing list, attempt-counted requeue, dispatcher (per-chat order, bounded, `/cancel` bypass, shutdown release) and tests.
## Phase 2: Approvals (US1)
- [x] T010 `telegram_approvals.py` and the `Bot` flow: ask, tap, decide, undo, stale pause, `/cancel`. `Door.cancel_turn(wait=True)`.
- [x] T011 Tests, incl. the cases where a tap must do nothing.
## Phase 3: Replies and commands (US4)
- [x] T020 `telegram_reply.py`; `/start`, `/help`, `/cancel`; non-text reply.
## Phase 4: Webhook (US3)
- [x] T030 `telegram_server.py` (webhook, secret, 200-after-push, 503), start-up checks, `deleteWebhook` in poll mode; alert `TelegramWebhookProbed`.
## Phase 5: Linking (US5)
- [x] T040 `telegram_link.py` (store, flow, `LinkedAuth`), the link pages, `--telegram-link-url` in the bootstrap script, alert `TelegramUpdatesDropped`.
## Phase 6: Docs
- [x] T050 Patterns 92 to 94, README, `.env.example`, CLAUDE.md pointers, this spec.

## Not done (disclosed)
- [ ] T060 Run against the real Telegram (webhook delivery, button taps, edit behaviour) and against Casdoor (refresh rotation, the redirect URI). HUMAN: needs a bot token and the gateway stack.
- [X] T061 (done in pattern 98: `deploy/caddy/gateway.caddy`, charged per address, and the bot as an opt-in prod service) Configure the reverse proxy to route `/telegram/*` to the bot without the front door's token (production Caddyfile; depends on spec 018 T069).
- [ ] T062 Rate-limit `/login`; load-test the dispatcher; a consumer-specific processing list for more than one bot process per domain.
