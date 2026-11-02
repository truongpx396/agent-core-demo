# Feature Specification: Telegram approvals, parallel chats, a webhook, growing replies and account linking

**Feature Branch**: `feat/telegram-approvals-linking-webhook`

**Created**: 2026-11-02

**Status**: Implemented in one PR, stacked on #226 (spec 020), at the maintainer's request that it not be split. It exceeds the ~1,000-line ceiling in CLAUDE.md for that reason, and the PR says so. Everything is verified against fakes; nothing was run against the real Telegram or a real Casdoor (research.md "NOT verified").

**Input**: The maintainer's request of 2026-11-02, after asking what a professional team does for Telegram: the account-linking flow (`/login` → authorization server → state matched back → "logged in"), human-in-the-loop approvals, and the gaps found in the channel (non-text messages ignored, no `/cancel`, no streaming, one chat at a time, no webhook), as ONE PR.

## Why

After spec 020 the Telegram channel is a real client of the front door, but it can only answer read-only questions, one chat at a time:

1. **Nothing that writes can be done.** Every turn is `unattended`; the worker declines each approval, so "save a note" ends in "needs a person's approval, which this channel can't provide". The backend already supports the pause and `POST /chat/resume`; the channel had no way to ask.
2. **One slow turn delays everyone.** The offset moves after the answer, in one sequential loop. `/cancel` cannot exist in that design, and a webhook (which Telegram redelivers if not answered quickly) cannot be built on it.
3. **Silence.** Photos, voice and files are ignored; there is no streaming; there is no way to stop a turn.
4. **Identity proves nothing about the human.** A `telegram-<id>` person (spec 020) is a separate account from the person's own, derived from a bot secret.

## User Scenarios & Testing

### User Story 1 - Approving an action from the chat (P1)
A person asks for something that writes. The bot shows each action and its arguments with Approve and Reject; their tap decides; the answer follows.
**Acceptance**: (1) the owner's tap resumes the turn once with their decision; (2) anyone else's tap does nothing and leaves the approval for its owner; (3) a second tap, or a redelivered callback, does not resume again; (4) a tap after expiry says so and cancels nothing; (5) a decision the door refused can be made again; (6) writing while an approval waits is pointed at the buttons and never sent to the agent; (7) after expiry the next message clears the stale pause and goes through; (8) if the buttons cannot be shown the pause is cancelled; (9) `TELEGRAM_APPROVALS=false` is the old unattended behaviour, unchanged.

### User Story 2 - A slow chat does not hold up the others, and `/cancel` works (P1)
**Acceptance**: (1) two chats run at once, one chat's updates in order; (2) `/cancel` runs while that chat's turn is running; (3) updates survive a crash and a poison update is dropped and counted after three tries; (4) a clean shutdown loses nothing.

### User Story 3 - The webhook (P2)
**Acceptance**: (1) a request without `TELEGRAM_WEBHOOK_SECRET` is refused and queued nowhere; (2) 200 only after the update is in Redis, 503 when it cannot be; (3) poll mode removes a leftover webhook; (4) start-up refuses a weak secret or a non-HTTPS address.

### User Story 4 - Replies that grow, and no more silence (P2)
**Acceptance**: (1) the reply is one message edited at most once per interval, and its final text is exact whatever the edits did; (2) non-text messages are answered; service messages are not; (3) `/start`, `/help`, `/cancel`.

### User Story 5 - Linking an account (P2)
**Acceptance**: (1) `/login` (private chat only) gives a one-time button to the authorization server; (2) the return asks "connect Telegram user X to account Y?" and binds nothing until the person presses the button; (3) a state works once, expires, and belongs to the Telegram id that asked; (4) an account of another tenant, or a login with no refresh token, is refused; (5) a person disabled at the authorization server is cut off at the next refresh and told to `/login`; (6) what is stored is sealed; (7) `/unlink`.

## Requirements

- **FR-001** Approvals by button, owner-only, once, expiring, as above. **FR-002** A stale pause is cleared (and waited for) by the next message. **FR-003** `/cancel` drops a waiting approval or asks the agent to stop. **FR-004** `callback_data` carries a random token only.
- **FR-005** Replies are edited in place, paced, with an exact last word. **FR-006** Updates go through a Redis inbox with a per-chat dispatcher, bounded concurrency and attempt-counted requeue. **FR-007** The webhook, authenticated by the `secret_token` header in constant time. **FR-008** Account linking as in User Story 5.
- **FR-009** Every degrade path is counted (`channel_telegram_*`), and the two that leave a person unanswered or signal a probe alert. **FR-010** Each new setting is in `Settings` and `.env.example`, and start-up refuses a half-configured bot.
- **FR-011** Nothing here reaches the queue or the graph (`tests/channels/test_channels_use_the_door.py`).

## Success criteria
SC-001 No test of User Stories 1 to 5 passes with the matching safeguard removed (ten were removed one at a time, plan.md). SC-002 `make lint`, `make typecheck` and the hermetic suite are green. SC-003 The README and GRAPH_PATTERNS 92 to 94 say what is and is not verified.
