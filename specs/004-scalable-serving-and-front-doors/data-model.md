# Data Model: Scalable Serving and Front Doors

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — read from the code, the compose files and `WORKER_CONCURRENCY.md`. This feature adds
**no tables**. What it owns is process shape, the Redis keyspace it uses, one persisted position, browser-side state
and a capacity model.

## 1. Front doors

| Door | Entry | Identity (`SecurityCtx`) | Who approves a pause | Runs the graph | Conversation id |
|------|-------|--------------------------|----------------------|----------------|-----------------|
| Web page | `GET /` → `POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel` | `X-Tenant-Id` / `X-Principal-Id` from editable selectors (trusted-layer headers) | a person, via buttons | in a worker | client-generated UUID (`crypto.randomUUID`, with a fallback) |
| HTTP stream | the same endpoints, any client | the same headers | the client | in a worker | client-supplied (default fresh UUID4) |
| Terminal | `python -m app.channels.chat [--hitl]` | tenant `DEFAULT_TENANT`, principal `local:<os user>` | a person, `Approve? [y/N]` (any pause; `--hitl` gates every tool call too) | in-process | fresh UUID per session |
| Chat app | `python -m app.channels.telegram` | tenant `DEFAULT_TENANT`, principal `telegram:<sender id>` (chat id if no sender) | nobody — auto-declined | in-process, unattended | `telegram:<chat id>` |

The terminal and the chat app are themselves the trust boundary (no network hop to spoof); the HTTP doors rely on a
gateway to set the headers (feature 002).

## 2. Worker process

One OS process: `python -m app.job_queue.agent_worker`.

| Field | Value | Notes |
|-------|-------|-------|
| `AGENT_DOMAIN` | fixed at startup; default `ecorp` | resolved first; unknown → raises with the valid names |
| `REQUESTS_STREAM` | `agent:requests:<AGENT_DOMAIN>` | one stream and one consumer group (`agent-workers`) per domain |
| `CONSUMER_NAME` | `<hostname>-<8 hex>` | unique per process start |
| concurrency bound | `AGENT_WORKER_MAX_CONCURRENCY` (10) | the semaphore *and* the `xreadgroup` `count` |
| read block | 5000 ms | the read is bounded by Redis, not a socket timeout |
| in-flight set | `set[asyncio.Task]` | drained on stop |
| stop signal | `asyncio.Event`, set by SIGTERM/SIGINT | checked between reads only |
| recovery loop | one task | every `WORKER_RECLAIM_INTERVAL_SECONDS` (feature 003) |

### 2.1 Lifecycle

```text
resolve domain ──▶ open graph + checkpointer on this loop ──▶ ensure group (id="0")
   ──▶ install SIGTERM/SIGINT ──▶ start recovery loop ──▶ ┌─ read (block 5 s, count = bound) ◀─┐
                                                          │  for each entry: acquire slot,      │
                                                          │  create task, track it              │
                                                          └─ stop set? ── no ─────────────────┘
                                                                  │ yes
        await all in-flight (each acks) ──▶ await recovery loop ──▶ close Redis ──▶ close appdata pool
                                                                  ──▶ close checkpointer pool ──▶ exit
```

A process killed rather than stopped skips everything after "stop set": its claimed entries stay pending until another
replica's recovery sweep claims them (feature 003).

### 2.2 Chat-app channel process

`python -m app.channels.telegram` — one domain (`AGENT_DOMAIN`), one bot token, **one message at a time**.

```text
token present? ──▶ resolve domain ──▶ open graph/checkpointer ──▶ load position (Redis) ──▶ install signals
   ──▶ loop: getUpdates(offset, timeout=30) ── error ──▶ sleep 5 s, retry
                  │ for each update:
                  │    text message? ─▶ typing action ─▶ run turn (unattended) ─▶ reply (split @ 4000)
                  │    advance offset = update_id + 1 ─▶ persist offset   ◀── only AFTER handling
        stop set ──▶ close appdata pool ──▶ close checkpointer pool
```

The `handle_message` call is **not** inside the poll's `try` — see B5.

## 3. Redis keyspace used by the serving tier

| Key / stream | Written by | Read by | TTL / bound | Feature that owns the semantics |
|--------------|------------|---------|-------------|---------------------------------|
| `agent:requests:<domain>` | API (`publish_*`), worker (`republish_job`) | workers (consumer group) | none (the stream is the queue) | 003 |
| `agent:requests:<domain>:dead` | worker (reclaim) | operators | `maxlen ≈ 1000`, approximate | 003 |
| `agent:results:<request_id>` | worker | API (SSE relay), possibly two de-duplicated readers | 300 s, refreshed on every write; **not** deleted on a terminal event | 003 |
| `agent:lock:<thread_id>` | worker | worker | `2 × REQUEST_TIMEOUT_SECONDS` (120 s) | 003 |
| `agent:cancel:<thread_id>` | API | worker (polled between events) | 60 s | 003 |
| `chat:submit_dedup:<thread_id>:<digest>` | API | API | `CHAT_SUBMIT_DEDUP_TTL_SECONDS` (10 s) | 003 |
| `telegram:offset:<domain>` | chat-app channel | chat-app channel | none | **004** |
| rate-limit counters | API middleware (via `limits`) | API middleware | one-minute moving window | 001 (`chat-turn-http.md`) |

`telegram:offset:<domain>` holds the integer `update_id + 1` of the last handled update; absent means "never polled
this domain" and is read as `0`.

## 4. Chat-app identity and conversation mapping

```text
Telegram update.message.chat.id  ──▶ thread_id  = "telegram:" + chat_id        (one per CHAT)
Telegram update.message.from.id  ──▶ principal  = "telegram:" + user_id        (one per SENDER)
                                      tenant     = DEFAULT_TENANT               (shared)
```

In a private chat `chat_id == user_id`, so thread and principal are 1:1. **In a group chat** many principals share one
thread: every sender's messages join one history, and each sender's memories stay their own (feature 002 B2; A6 here).
The `telegram:` thread-id prefix is shared with `app/agent/sessions.py` (`TELEGRAM_THREAD_PREFIX`) because HTTP callers
may never *claim* an id in that namespace.

## 5. Browser state

Held only in the user's browser; never sent anywhere. Every access is wrapped in a try/catch so a locked-down context
degrades to defaults.

| `localStorage` key | Content | Used by |
|--------------------|---------|---------|
| `agent-core-demo:theme` | `"light"` / `"dark"` | theme toggle |
| `agent-core-demo:tenants` | recently used tenant ids | tenant selector |
| `agent-core-demo:principals` | recently used principal ids | principal selector |

In-memory only: the active `thread_id`, the active turn (`activeTurn`: thread, phase, event handler), the attached image.

## 6. Capacity model

### 6.1 Concurrency

`turns served at once = workers_in_domain × AGENT_WORKER_MAX_CONCURRENCY`, **capped by the model backend's own
concurrency** (a native Ollama serves one generation at a time; the fake server in `loadtest/` answers many). The
checkpointer adds a per-process cap: at most `CHECKPOINTER_POOL_MAX_SIZE` checkpoint operations in flight (the
semaphore that replaces the saver's lock).

### 6.2 Redis connections

Each open SSE stream holds one pooled connection for the whole turn, so concurrent streams ≈ connections held. The pool is
`REDIS_MAX_CONNECTIONS` (300); the library default (100) failed ~83% of 250 concurrent requests.

### 6.3 Postgres connections

```text
worst case ≈ 20 × (agent-worker replicas + API replicas)        # appdata pool 10 + checkpointer pool 10 each
need        ≤ max_connections − reserved                         # default 100 − 3 reserved → about four processes
```

LiteLLM and Langfuse share the same server. Exhaustion shows as `FATAL: sorry, too many clients already`, turns failing
*inside* an open stream, and an HTTP 200 with no answer text. The integration container is given `max_connections=300`.
The appdata pool's `max_size=10` is not configurable.

## 7. Settings reference

Defaults read from `app/core/config.py` (`Settings`). **Example env** says whether the key is in `.env.example` or
`.env.prod.example` — the project rule is that every tunable is (A3).

| Setting | Default | What it bounds | Example env |
|---------|---------|----------------|-------------|
| `agent_domain` | `ecorp` | which domain this process serves | yes |
| `agent_worker_max_concurrency` | 10 | turns per worker process | **no** |
| `ingest_worker_max_concurrency` | 10 | ingest jobs per process (feature 006) | **no** |
| `redis_max_connections` | 300 | pooled Redis connections per process | **no** |
| `redis_url` | `redis://localhost:6379` | where the queue lives | **no** |
| `checkpointer_pool_max_size` | 10 | checkpointer pool and its semaphore | **no** |
| `rate_limit_per_minute` | 30 | per-tenant turn-creating requests | production file only |
| `chat_submit_dedup_ttl_seconds` | 10 | identical-resubmission window | **no** |
| `chat_first_response_deadline_seconds` | 30 | wait for a job's first event | **no** |
| `request_timeout_seconds` | 60 | one turn's wall clock | **no** |
| `worker_reclaim_interval_seconds` | 60 | recovery sweep cadence | **no** |
| `agent_worker_reclaim_idle_seconds` | 240 | idle time before an entry is presumed abandoned | **no** |
| `max_auto_reclaim_retries` | 1 | recovery retries before dead-lettering | **no** |
| `cors_allowed_origins` | `*` | browser origins allowed | yes (production narrows it) |
| `telegram_bot_token` | empty | refuses to start when empty | yes |
| `default_tenant` | `ecorp` | the tenant of the terminal and chat-app doors | **no** |
