# Contract: Chat Turn over HTTP

**Feature**: [spec.md](../spec.md) | **Related**: [turn-event-stream.md](./turn-event-stream.md), [error-envelope.md](./error-envelope.md)

**Status**: Retrospective — read from `app/api/main.py`, `app/api/schemas.py`,
`app/api/rate_limit.py`, `app/api/health.py`. This contract covers the endpoints that *start* a
turn and report liveness. Resume / cancel / pending-approval are feature 003; session list,
transcript and `/usage` are feature 002 / cost governance; `/ingest/*` is the ingestion feature.

## Identity and routing headers (every chat endpoint)

| Header | Required | Behavior |
|--------|----------|----------|
| `X-Tenant-Id` | **yes** | Absent → **422** before the handler runs (fail closed by the *shape* of the dependency). |
| `X-Principal-Id` | **yes** | Same. |
| `X-Domain` | no (default `ecorp`) | Unknown value → **422** `Unknown X-Domain '<x>' — must be one of: …`. Selects which domain's worker pool serves the turn. |

These are *trusted-layer* headers, **not authentication**: nothing verifies them, and the
service is only safe behind a gateway that authenticates the caller, sets them itself and strips
client-supplied copies. There is deliberately no body field that can set identity and no default
identity. (Isolation semantics: feature 002.)

## `POST /chat/stream/queued` — start a turn

**Request body** (`ChatRequest`, JSON):

| Field | Type | Rules |
|-------|------|-------|
| `message` | string | required, `min_length = 1` (an *empty* string is a 422; a whitespace-only string passes the schema and is handled in-graph — see the empty-input rule below) |
| `thread_id` | string | optional; defaults to a fresh UUID4. Reuse to continue a conversation. **Client-supplied** — see the ownership gap in feature 002. |
| `images` | string[] | optional, default `[]`; image URLs or data URIs, passed to the model untouched, never fetched. |

**Response**: `200`, `Content-Type: text/event-stream`, headers `Cache-Control: no-cache`,
`X-Accel-Buffering: no`. Each event is one frame `data: <json>\n\n` using the vocabulary in
[turn-event-stream.md](./turn-event-stream.md). The HTTP status is `200` even when the *turn*
fails — failure is reported in-stream as an `error` event, because the status line is sent
before the worker has produced anything.

**Behavior guarantees**

1. **Queued, not in-process.** The endpoint never runs the graph; it publishes a job on the
   per-domain Redis stream and relays whatever the worker publishes for that request id. At least
   one worker for the domain must be running.
2. **Submission de-duplication.** An identical `(thread_id, message, images)` resubmission within
   `CHAT_SUBMIT_DEDUP_TTL_SECONDS` (default 10 s) reuses the first attempt's request id and
   stream instead of starting a second turn. If the publish fails after the claim wins, the claim
   is released (compensating delete) so a retry claims fresh.
3. **First-event deadline.** If *no* event arrives within
   `CHAT_FIRST_RESPONSE_DEADLINE_SECONDS` (default 30 s) the stream yields one `error` event and
   ends. The deadline clears the instant the first real event arrives; a slow-but-alive turn is
   governed by the worker-side 60 s turn limit, not this one. (This `error` has no `code` — see
   error-envelope.md.)
4. **One active turn per conversation.** A second job for a `thread_id` that is already running
   is rejected immediately with `error{code: "thread_busy"}`; it is not queued behind the first.
5. **Terminal event.** The stream ends after the first of `done`, `error`, `approval_required`.
6. **Results stream lifetime.** The per-request results stream is *not* deleted when a reader sees
   the terminal event (two de-duplicated callers may legitimately read the same stream); it
   expires by TTL.

**Rate limiting** (`TenantRateLimitMiddleware`): `/chat/stream/queued`, `/chat/resume` and
`/ingest/upload` only — never health, reads, or `/chat/cancel`. Keyed by `X-Tenant-Id` (client
address if absent), moving window of `RATE_LIMIT_PER_MINUTE` (default 30). Over the limit →
**429** `{"detail": "Rate limit exceeded: 30 requests per minute per tenant"}` and
`agent_rate_limit_exceeded_total` increments. If the limiter's Redis is down it **fails open**.

**Status codes**

| Code | When |
|------|------|
| 200 | stream opened (turn outcome is in-stream) |
| 422 | missing `X-Tenant-Id` / `X-Principal-Id`; unknown `X-Domain`; body fails schema (e.g. empty `message`) |
| 429 | per-tenant rate limit |
| 5xx | unhandled server error before the stream opens (a publish failure re-raises after releasing the dedup claim) |

**Empty-input rule (in-graph).** A message with no text *and* no image yields, in-stream, the
assistant text "I didn't receive a question — please try again." followed by `done`; it is not an
HTTP error.

## `GET /health` — liveness

`200 {"status": "ok"}` whenever the process can respond. No dependency checks.

## `GET /health/ready` — readiness

Probes five dependencies concurrently, each bounded to 2 s. `200` only if all pass, otherwise
`503`; body `{"status": "ready" | "degraded", "checks": {…bool…}}` with keys exactly:
`qdrant`, `appdata_postgres`, `checkpointer_postgres`, `redis`, `ml_service`. `ml_service` is
reported even though its consumers degrade rather than fail (re-ranking falls back to fused
order; moderation to patterns only), so a degraded readiness does not mean turns fail.

## `GET /` — built-in web UI

A single self-contained page that speaks only the contract above (no special-cased endpoint).
