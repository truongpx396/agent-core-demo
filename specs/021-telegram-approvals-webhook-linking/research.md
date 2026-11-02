# Research: what was read, what was run, what was not

## Read (Telegram Bot API, https://core.telegram.org/bots/api, 2026-11-02)
- `setWebhook`: `url` is HTTPS; `secret_token` is 1-256 characters of `A-Z a-z 0-9 _ -` and is sent back in `X-Telegram-Bot-Api-Secret-Token`; `allowed_updates`; `drop_pending_updates`; a non-2xx answer is retried.
- `getUpdates` does not work while a webhook is set; `deleteWebhook` takes `drop_pending_updates`.
- `sendMessage`: `text` 1-4096 characters, `reply_markup` accepts an inline keyboard. `InlineKeyboardButton.callback_data` is 1-64 bytes; `url` is HTTP or `tg://`.
- `CallbackQuery`: `id`, `from`, `message`, `data`. `answerCallbackQuery`: `text` 0-200 characters, `show_alert`.
- `User.id` has at most 52 significant bits (fits a Python int). Usernames: the page did not say, so nothing keys on one.

## NOT found on the pages read
- What `editMessageText` answers when the new text equals the old; any per-chat rate limit on edits. The code relies on neither: a failed edit is ignored and the final text is always sent or edited in last; edits are paced by a configurable floor.
- Whether an `editMessageText` that omits `reply_markup` removes the keyboard. The code always states it.

## Run
Everything under `tests/channels/` against fakes (Telegram, the door, Redis, the authorization server's code exchange and refresh); the webhook and link pages through an in-process ASGI client; ten mutations (plan.md). The Redis fake covers `SET NX/EX`, `GETDEL`, `LPUSH/RPUSH/LREM/LRANGE` and `BLMOVE`; the installed redis-py (8.1.0) has `blmove` and `getdel`.

## NOT verified
- **Nothing was sent to or received from the real Telegram** (no bot token was used).
- **Casdoor was not run for this feature.** Whether a refresh rotates the refresh token is not measured (both outcomes are handled); the redirect URI for the bot's page is registered by the bootstrap script, which was tested against its fake, not against Casdoor.
- No load test of the dispatcher; no Redis restart test of the processing list.
- The proxy routing for `/telegram/*` (no front-door token) is documented, not configured: the production Caddyfile is not wired to the door at all yet (spec 018, T069).
