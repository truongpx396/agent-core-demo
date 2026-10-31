# Tasks: the CLI and the Telegram channel go through the front door

`[x]` done and verified as the line says; `[ ]` not done. Phase = a natural PR boundary if this is ever split (plan.md).

## Phase 0: Research
- [x] T001 R1 against a real Casdoor 4.24.0 (research.md): grants, refusals, what application credentials can do, token exchange, headless code flow, disabled persons.

## Phase 1: API (FR-007, FR-009, FR-015)
- [x] T010 `ChatRequest.unattended`, `require_approval`, `replay_key` (bounded 1-200) and their pass-through to the job.
- [x] T011 Replay claim keyed by the key, lasting `RESULTS_STREAM_TTL_SECONDS`; released on a failed publish.
- [x] T012 A `telegram:` id is claimable only by the caller it names, compared with its separator.
- [x] T013 Tests: flags reach the job; replay is one job; a different key is another; the claim's window; another person cannot replay into the owner's turn; the id rule incl. the separator.

## Phase 2: Door client and the CLI (FR-001, FR-002, FR-003, FR-011, FR-012, FR-013, FR-016, FR-017)
- [x] T020 `app/channels/door.py` (`Door`, `HeaderAuth`, refusal words, metrics) and its tests, incl. against the real API in process.
- [x] T021 `app/channels/oauth.py` (PKCE, headless sign-in, refresh) and tests against a fake that reproduces what was measured.
- [x] T022 `app/channels/cli_auth.py`: browser + real loopback listener, state check, mode-600 session, silent refresh, sign-out.
- [x] T023 `app/channels/chat.py` as a door client (`--sign-out`, header mode); tests.
- [x] T024 The bootstrap registers the CLI callback (`--cli-callback-port`); test pins it to the setting's default.
- [x] T025 Settings and `.env.example`: `CHANNEL_*`, `TELEGRAM_*` (`tests/core/test_env_example_matches_settings.py` passes).

## Phase 3: Telegram people (FR-004, FR-005, FR-006, FR-008, FR-010, FR-015)
- [x] T030 `app/channels/telegram_people.py`: derived password, sign-in, opt-in creation under a per-person lock, disabled is final.
- [x] T031 `scripts/telegram_person.py` and `make telegram-person` (add / disable / enable; prints the revoke line); tests.
- [x] T032 `app/channels/telegram.py` as a door client: a door per user, `telegram:<person>:<chat>`, refusals in words, update number as `replay_key`; tests.

## Phase 4: Remove the old path and say so (FR-001, FR-018)
- [x] T040 Delete `app/job_queue/producer.py` and its tests; the channels bind no port (EXEMPT in `test_entrypoints_wire.py`, the composition-root floor lowered with the reason).
- [x] T041 `tests/channels/test_channels_use_the_door.py` (the checker is itself tested: it once missed `from app.job_queue import producer`).
- [x] T042 GRAPH_PATTERNS.md 89 rewritten, 91 added; README (diagram, front-door and channels sections, limits), CLAUDE.md, code comments.
- [x] T043 Mutation check: 16 deliberate defects each fail a test (two did not at first; the tests were fixed).

## Not done
- [ ] T050 **Through the real door**: Caddy + authlimit + API + worker with a Telegram person's token and the CLI's (SC-002 live, SC-003, SC-006, SC-009). Extend `scripts/authlimit_smoke.py --upstream api` with a person from `telegram_people` and a replayed `replay_key`.
- [ ] T051 A real Telegram bot and a real browser sign-in.
- [ ] T052 The opt-in provisioning against real Casdoor (its credentials were shown able to; the bot's code path was tested against the fake).
- [ ] T053 (HUMAN) Production: wire the Caddyfile and compose to the door (spec 018 T069); choose whether `TELEGRAM_AUTO_PROVISION` ever goes on and then give the bot a tenant of its own.
