# Implementation Plan: the CLI and the Telegram channel go through the front door

**Spec**: [spec.md](spec.md) · **Research**: [research.md](research.md) · **Tasks**: [tasks.md](tasks.md) · **Branch**: `feat/channels-behind-the-front-door` · **PR**: #226

## Summary

Both channels stop publishing to the work queue from their own process and become clients of `POST /chat/stream/queued`, `/chat/resume` and `/chat/cancel` through the front door, each as a real person at the authorization server. The API gains the three request fields the channels used to decide in-process (`unattended`, `require_approval`, `replay_key`) and a thread rule that lets a `telegram:` id be claimed by the caller it names. Research (R1) changed the Telegram design: the bot derives a password per person from one secret and cannot create people by default.

## The design in six decisions

1. **One client module, `app/channels/door.py`.** It yields the API's own event vocabulary and turns every refusal into one `error` event, so `chat.py` and `telegram.py` keep their renderers. No other route exists: an unreachable door sends nothing.
2. **Sign-in is the authorization-code flow, with PKCE and `resource`.** The CLI uses a browser and a loopback listener (`cli_auth.py`); the bot posts the person's credentials headlessly (`oauth.py`). The password grant cannot be used (R1 T1).
3. **A Telegram person's password is derived, never stored** (`telegram_people.py`): `HMAC(TELEGRAM_PERSON_SECRET, tenant, id)`.
4. **Creating a person is an operator step by default** (`make telegram-person`); the bot creating one is the opt-in `TELEGRAM_AUTO_PROVISION` (R1 T3).
5. **Request fields that can only tighten**: `unattended`, `require_approval`, `replay_key`. The replay claim lasts as long as the results stream and is scoped to the conversation; the ownership check runs before it.
6. **A Telegram conversation id names its owner**: `telegram:<person>:<chat>`, claimable only by that person.

## Constitution Check

| Principle | Touched | How it holds |
|---|---|---|
| **I. Fail-closed tenant isolation** (NN) | Yes, the point | A channel supplies no tenant or person: in identity mode the API trusts only the identity the door signs from the token (`owner`, `sub`), and ignores the channels' own headers like anyone's. An unknown, disabled or unreachable sign-in is a refusal, never a default tenant. The old direct-to-queue path, where the process named any tenant, is deleted and `tests/channels/test_channels_use_the_door.py` fails the build if it returns. The reserved Telegram namespace is claimable only by the person it names; a replay key cannot reach another person's turn (the ownership check precedes the claim; test). **Residual, stated:** a leaked `TELEGRAM_PERSON_SECRET` acts as the Telegram people anywhere in their tenant (R1 T6), and with `TELEGRAM_AUTO_PROVISION` the bot holds a tenant-wide credential (R1 T3); neither crosses a tenant. |
| **II. Mandatory approval** (NN) | Yes | `unattended` can only make the worker decline a pause; no field of the request approves a call or skips the gate (API test: the flags reach the job and nothing else changes). `require_approval` adds approvals. The channel still cancels a pause it receives, so an old worker cannot leave a conversation paused. |
| **IV. Exactly-once side effects** (NN) | Indirectly | A redelivered Telegram update is one turn (`replay_key`), which is what kept it from being a second model call and a second write attempt. The tool-level idempotency guard is untouched. |
| V. Bounded, observable failure | Yes | Every refusal reaches the person in words and is counted (`channel_door_refusal_total{channel,reason}`, `channel_signin_total`, `channel_person_provisioned_total`); no log, label or error carries a token (tested for the session file and the CLI's output). No alert rule was added: the door's own refusals already alert. |
| VII. Real backends where they matter | Yes | The Casdoor-dependent code was run against the real 4.24.0 (research.md "Verified live"); the door client against the real FastAPI app in process. The full door stack was **not** run (research.md "NOT verified"). |
| VIII. Why-first docs, honest gaps | Yes | GRAPH_PATTERNS.md 89 rewritten and 91 added with their gaps; the README says what is and is not limited; the spec was corrected where R1 overturned it. |

## Structure

```text
app/channels/   door.py (client, SSE, refusals)  oauth.py (PKCE, headless sign-in, refresh)
                cli_auth.py (browser + loopback listener, mode-600 session)
                telegram_people.py (derived passwords, opt-in creation)  chat.py  telegram.py
app/api/        schemas.py (3 fields)  routers/chat.py (replay key, thread rule)
scripts/        telegram_person.py (operator)  mcp_gateway_bootstrap.py (registers the CLI callback)
deleted         app/job_queue/producer.py, tests/job_queue/test_producer.py
tests/channels  fake_casdoor.py (as measured)  test_door  test_door_against_the_api  test_oauth  test_cli_auth
                test_telegram_people  test_telegram_channel  test_chat_cli  test_channels_use_the_door
```

## Size and delivery (SC-012)

One PR, at the maintainer's request, far over the ~1,000-line ceiling (see the PR description for the count). If it must be split, the pieces that stand alone are, in order: (1) the API fields and the thread rule with their tests; (2) the door client, `oauth.py`, `cli_auth.py` and the CLI; (3) the Telegram people, the operator tool and the Telegram channel; (4) the docs and the deletion of the producer.
