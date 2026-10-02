# Quickstart: Validate Scalable Serving and Front Doors

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves the queue and worker logic against a fake Redis, the API handlers, the rate
> limiter, and the chat-app channel's mapping, formatting and position handling. It does **not** prove capacity (Tier 2),
> graceful shutdown (no test exists — A1), the terminal (no test exists — A2), or the web page's behavior (A7) — and it did
> **not** catch B5 or B6, which Scenarios B5 and B6 below reproduce. Those two scenarios are expected to show the defect
> *as the system stands*.

---

## Tier 1 — Hermetic (no services, ~2 s)

```bash
pytest tests/job_queue tests/api tests/channels -q
```

**Expected** (observed 2026-10-02): `195 passed`.

| Requirement | Evidence |
|-------------|----------|
| FR-001/FR-014 the HTTP path publishes on the domain's stream and relays results; routing by `X-Domain` | `tests/api/test_api.py::TestChatStreamQueued` (incl. `test_a_non_ecorp_domain_publishes_onto_its_own_stream`), `tests/job_queue/test_queue.py::TestPublishRequest` |
| FR-014 one delivery per job via a consumer group; group created from the beginning | `tests/job_queue/test_queue.py::TestEnsureConsumerGroup`, `tests/job_queue/test_agent_worker.py::TestRunLoop` |
| FR-015 concurrency is bounded **and** actually overlaps | `test_agent_worker.py::TestConcurrentDispatch::test_bounds_concurrency_and_actually_overlaps` |
| FR-018 no socket timeout on blocking reads | `test_queue.py::TestGetClient::test_disables_the_socket_timeout_so_a_blocking_read_cannot_race_it` (the **`max_connections`** size has no hermetic test — Tier 2) |
| FR-027 rate limit: limit, per-tenant budgets, fail-open, metric, unrated paths | `tests/api/test_rate_limit.py` |
| FR-028 readiness reports each store; a hung check is bounded | `tests/api/test_health.py` |
| FR-002 the page sends identity and domain headers, starts fresh on identity change | `tests/api/test_api.py::TestUi` (HTML substring checks only — A7) |
| FR-006/FR-008 mapping, sources footer, splitting, no-silence fallback, non-text skipped, a failed send swallowed | `tests/channels/test_telegram_channel.py` (`TestThreadAndCtx`, `TestFormatReply`, `TestSendMessage`, `TestHandleMessage`) |
| FR-009 position loaded, persisted after each message, scoped per domain | `test_telegram_channel.py::TestOffsetPersistence`, `TestRunPersistsOffsetAcrossPolls` |
| FR-010 refuses to start without a token; resolves the domain before polling | `test_telegram_channel.py::TestRun` |
| FR-016 same-thread jobs exclude each other; different threads never contend | `test_agent_worker.py::TestSameThreadJobsAreSerialized` |

**Not covered here**: FR-013 and FR-022 (graceful stop of the channel and the worker — A1), FR-005 (the terminal — A2),
FR-003/FR-004 (the page's event handling — A7), FR-007 and FR-012 (B6, B5).

---

## Tier 2 — Real Postgres and Redis, real processes (Docker, no model)

```bash
make test-integration
```

**Expected**: passes or self-skips if Docker is unreachable. Relevant: `tests/integration/test_worker_scaling.py` — 5 real
worker processes × concurrency 50 behind a real HTTP server and a fake model server: **250 concurrent turns** all return the
correct answer (**FR-024, SC-001**), a concurrent approve round trip writes to a real Qdrant, and a concurrent delegation
run completes; `tests/integration/test_queue_real_redis.py` (the producer/consumer round trip against a real Redis Stack).
*Not run while writing this spec.* **Nothing here exercises a non-default domain (A8) or a graceful stop (A1).**

---

## Scenario B5 — Reproduce: one message whose turn raises ends the chat-app channel (hermetic; expected: it reproduces)

In a scratch Python session (do not commit), with no services:

1. Patch `app.channels.telegram`: `TELEGRAM_BOT_TOKEN = "x"`; `httpx.AsyncClient` → a fake whose `get` returns one update
   (`update_id` 10, a text message), and whose `post` returns an empty response; `astream_events_turn_unattended` → an async
   generator that **raises** `RuntimeError` before yielding; `init_graph_async`, `sql_store.close_pool` and
   `close_checkpointer_pool` → no-ops; `resolve_domain` → a stub; `get_redis_client` → `FakeRedis()` from
   `tests/job_queue/test_queue.py`.
2. `await telegram.run()`.
3. Read `telegram:offset:ecorp` from the fake Redis.

**Observed 2026-10-02**: `run()` **raises** the `RuntimeError` (the loop did not survive); the stored position is **`None`**;
the only offset polled was `0`, so a restart polls the same update again. **Fixed when**: `run()` keeps polling after a
message whose turn raises, the user gets a reply, and the position is advanced (or the repeat is bounded). This is the
failing test the B5 fix starts with.

## Scenario B6 — Reproduce: the terminal and the chat app concatenate a rejected draft with the retried answer (hermetic; expected: it reproduces)

1. For the chat app: patch `telegram.astream_events_turn_unattended` to yield
   `token "The refund window is 90 days. "`, `retry`, `token "The refund window is 30 days."`, `done`; call
   `await telegram._run_turn("refund window?", "telegram:1", ctx)`.
2. For the terminal: pass the same four events as an async generator to `app.channels.chat._render_stream`, capturing stdout.

**Observed 2026-10-02**: both produce `"The refund window is 90 days. The refund window is 30 days."` (the terminal followed by
a newline). **Fixed when**: the chat app returns only `"The refund window is 30 days."`, and the terminal shows the corrected
answer without the rejected one (or marks the discard). This is the failing test the B6 fix starts with.

## Tier 3 — Full local stack, manual walk-through

**Prerequisites**: `make up`, `make pull-models`, `make ingest`, and a worker per domain you test:
`make agent-worker`, `make agent-worker-support`. Helper (a function — unquoted variables are not word-split in zsh):

```bash
ask() { curl -N -X POST "localhost:8000/chat/stream/queued" -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: ecorp' -H 'X-Principal-Id: alice' -H "X-Domain: ${3:-ecorp}" \
  -d "{\"message\":\"$1\",\"thread_id\":\"$2\"}"; }
```

| # | Do | Expected | Proves |
|---|----|----------|--------|
| 1 | `make serve`, then `ask "what is 2+2?" s-1` | a stream ending `done`; no worker running → `error` "No response … is an agent-worker running for this domain?" after ~30 s | FR-001, SC-005 |
| 2 | start a second `make agent-worker` and fire 20 requests in parallel | all complete; both workers' logs show `agent_worker_started` with different consumer names | FR-014 |
| 3 | `ask "hello" s-2 support` with only the ecorp worker running | the 30 s no-response error (no support worker) | FR-014 (per-domain pools) |
| 4 | `AGENT_DOMAIN=nosuch make agent-worker` | exits at once with `Unknown AGENT_DOMAIN 'nosuch' — must be one of: …` | FR-021, SC-004 |
| 5 | while a slow turn is in flight, `kill -TERM <worker pid>` | log `agent_worker_stopping` with `in_flight ≥ 1`; the turn still finishes and the stream ends `done`; then the process exits | FR-022, SC-002 *(manual — no test, A1)* |
| 6 | open `http://localhost:8000/`, send a message, switch the **Principal** selector, send again | the second message starts a new conversation (empty transcript), not a 404 | FR-002 |
| 7 | in the page, ask for a side effect (e.g. add a note) | an Approve / Reject choice appears; Approve completes it; Stop cancels | FR-003, FR-004 |
| 8 | `make chat`, ask for a side effect (without `--hitl`) | `Approve? [y/N]` appears | FR-005 |
| 9 | `make telegram` (token in `.env`), message the bot twice, `kill -TERM` and restart it | the earlier messages are **not** answered again | FR-009, SC-003 |
| 10 | send the bot a photo | no reply, no error in the log | FR-008 |
| 11 | message the bot a long grounded question | the reply arrives in parts, with a `Sources:` list at the end | FR-006, FR-008 |

### Load test (optional)

```bash
make loadtest-up        # fake model server + points the containers at it
make loadtest-queued-headless
```

**Expected**: a CSV/HTML report under `loadtest/results-queued/`; the "Agent Core Overview" dashboard panels move. (`make
loadtest-down` restores the real model.) Not run while writing this spec.

## Checking the alerts

`RateLimitRejectionSpike` and `ScrapeTargetDown` are rule-file entries (`observability/prometheus/alerts.yml`). There is **no**
alert for a dead-lettered or lost-worker job, and no queue-depth metric (A4).

## Troubleshooting

- *Requests hang for 30 s then report "is an agent-worker running for this domain?"*: no worker is running for the
  `X-Domain` you sent.
- *Turns return HTTP 200 with no text under load*: suspect Postgres connection exhaustion (`too many clients already` in the
  worker logs) before suspecting the model — data-model §6.3.
- *`MaxConnectionsError` in the API logs*: `REDIS_MAX_CONNECTIONS` is below the number of concurrent streams.
- *A worker container shows as unhealthy in `docker ps`*: expected from the image-level probe (A4).
- *Scenario B5/B6 do not reproduce*: confirm you are not on a branch that already fixed them.
- *Do not* run `make clean`, `clear-*` or `restart-all` while validating.
