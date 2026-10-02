# Contract: Chat-App Channel

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §2.2, §4](../data-model.md) | **Events**: feature 001
[turn-event-stream.md](../../001-core-rag-agent-turn/contracts/turn-event-stream.md)

**Status**: Retrospective — `app/channels/telegram.py`; tests in `tests/channels/test_telegram_channel.py`. **B5 and B6
below are open defects**, marked where they break the contract.

## Invocation and configuration

`python -m app.channels.telegram` (`make telegram`, `telegram-support`, `telegram-sales`). `TELEGRAM_BOT_TOKEN` is
required — the process **refuses to start** without it (`RuntimeError`). `AGENT_DOMAIN` picks the domain it boots as.
One domain, one bot token, one process. Not containerized (A9).

## Inbound

| Property | Behavior |
|----------|----------|
| Transport | long-poll `getUpdates(offset, timeout=30)`; no public URL needed |
| Poll error | caught, logged `telegram_poll_failed`, wait 5 s, retry — the loop continues |
| Message kinds | only `message.text` is handled; photos, stickers, voice are skipped **but their position is advanced** |
| Handling order | strictly one message at a time (a slow turn delays every chat) |
| Thread | `telegram:<chat id>` (one per chat) |
| Principal / tenant | `telegram:<sender id>` (chat id if there is no sender) / the default tenant |

## Position (offset) — at-least-once

1. Loaded at startup from `telegram:offset:<AGENT_DOMAIN>` (absent → `0`).
2. After **each** update is handled: `offset = update_id + 1`, then persisted. Never before.
3. A crash between handling and persisting repeats **one** message (a duplicate reply) on restart.
4. A restart never resets to `0` (it used to; every update Telegram still remembered was replayed to real users).

## Outbound

1. A `typing` chat action is sent first (best-effort; failure ignored).
2. The turn runs through `astream_events_turn_unattended` (feature 003): any approval pause is **declined**, never approved.
3. The reply is the concatenated `token` events, or the `error` event's `content` if one arrives; if that is empty, a fixed
   fallback ("I wasn't able to put together a reply to that just now — could you try again?") — a user is never left
   with silence.
4. If the answer has `citations`, a `Sources:` list (`<marker> <title or doc id>`) is appended.
5. Replies longer than **4000** characters are split into consecutive messages (not truncated).
6. A failed send is logged `telegram_send_failed` and skipped; it does not stop the loop. It is **not counted** (A6).

## Events the channel consumes

| Event | Handling |
|-------|----------|
| `token` | appended to the reply |
| `citations` | kept for the `Sources:` list |
| `error` | its `content` becomes the reply |
| `retry` | **ignored — contract violation (B6)**: the rejected draft stays in the reply |
| `system_note`, `compacted`, `followups`, `tool_*` | ignored |
| `done` | seen and ignored |
| `approval_required` | never reaches the channel — the unattended wrapper swallows the pause and resumes with a decline |

## Failure policy

| Failure | Required | As built |
|---------|----------|----------|
| missing token | refuse to start | ✔ |
| unknown domain | refuse to start | ✔ (`resolve_domain` raises) |
| poll error | continue after a back-off | ✔ |
| send error | continue | ✔ (uncounted — A6) |
| a turn that **raises** | log, count, reply generically, advance, continue | **✘ the process ends and the position is not advanced (B5)** |
| stop signal | stop polling, finish the message in hand, close both pools | ✔ bounded by the 30 s poll window |

## Invariants a change must preserve

1. Persist the position only after the message is handled.
2. Never auto-approve; a reply is always sent.
3. The thread is per chat and the principal per sender; the `telegram:` prefix stays reserved against HTTP claims.
4. Honor every obligation in feature 001's client list — including `retry`.
5. A failure while handling one message must not take down the others.
