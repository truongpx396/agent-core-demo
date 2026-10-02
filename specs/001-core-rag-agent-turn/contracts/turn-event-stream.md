# Contract: Turn Event Stream

**Feature**: [spec.md](../spec.md) | **Transport**: [chat-turn-http.md](./chat-turn-http.md) | **Errors**: [error-envelope.md](./error-envelope.md)

**Status**: Retrospective — produced by `app/agent/runtime_stream.py::_run_graph_stream` (and the
`astream_events_*` entry points that wrap it); relayed verbatim by the worker onto the results
stream and by the API as SSE frames. The same vocabulary is consumed by the web UI, the CLI and
the Telegram channel.

Every event is a JSON object with a string `type`. Unknown `type` values MUST be ignored by
clients (the vocabulary has grown before and may again).

## Events

| `type` | Shape (besides `type`) | Terminal? | Meaning |
|--------|------------------------|-----------|---------|
| `token` | `content: string` | no | A chunk of the **main answer**, from the `agent` node only. Also used **synthetically**, once, to carry final text from a node that never calls the model (refusal, cache hit, safety-net fallback, replaced answer). |
| `tool_start` | `tool: string`, `args: object`, optional `subagent: string` | no | A tool began. `subagent` is present iff the call happened inside a delegated sub-run. |
| `tool_end` | `tool: string`, optional `subagent: string` | no | A tool finished. No result payload (results go to the model, not the client). |
| `retry` | — | no | **Discard everything rendered so far for this answer.** Sent when the quality gate rejected the answer, when a missing citation marker was inserted after the answer streamed, or when retry exhaustion replaced it. A synthetic `token` with the replacement text may follow immediately. |
| `compacted` | — | no | Informational: older turns were just summarized. Fires only when compaction actually removed something. |
| `system_note` | `content: string` | no | Informational notice (e.g. an earlier approval could not be resumed after an app update; starting a new request). |
| `citations` | `items: Citation[]`, `ungrounded_claims_count: int` | no | Sent immediately before the terminal event iff `items` is non-empty **or** `ungrounded_claims_count > 0`. `items` are the *used* sources only. |
| `followups` | `items: string[]` | no | Sent immediately before the terminal event iff non-empty (2–3 questions; empty for an ungrounded answer and for a cache-served turn). |
| `approval_required` | `tool_calls: [{name, args}]` | **yes** | The turn paused at the approval gate (feature 003). |
| `done` | — | **yes** | The turn finished. |
| `error` | `content: string`, and — when the path uses the envelope — `code`, `message`, `details` | **yes** | The turn ended in failure. See error-envelope.md for which paths carry `code`. |

`Citation` = `{marker: "[n]", doc_id: string, title: string, text: string, score: number}`
(data-model.md §2).

## Ordering rules

1. **Exactly one terminal event** (`approval_required` | `done` | `error`) ends every stream, and
   it is always last.
2. `citations` then `followups` (each only if non-empty) come **immediately before** the terminal
   event, in that order.
3. `retry` is only ever followed (eventually) by fresh `token` events or by a terminal event; a
   client that has rendered text MUST clear it on `retry`. Without this, the rejected and the
   retried answer render concatenated with no separator (a real bug this event fixes).
4. A `retry` caused by marker auto-insert or by retry exhaustion is followed by a synthetic
   `token` carrying the full replacement text; a `retry` caused by `retry_output` is followed by
   the next `agent` round's ordinary `token` events.
5. Retry exhaustion for a **trusted** reason (missing marker; short-but-non-blank answer) emits
   **no** `retry` and no replacement: the already-streamed text stands as the final answer. (Firing
   `retry` there would blank a good answer with nothing to follow it.)
6. Events from a delegated sub-run's *reasoning* never appear as `token`; only its `tool_start` /
   `tool_end` appear, tagged with `subagent`.
7. `token` events from `suggest_followups` and `compact_history` model calls are filtered out;
   only the `agent` node's chunks are tokens.
8. A turn served by a node that never calls the model still yields its text as one `token`
   before `done` (a bare `done` with no answer is a bug, not a valid stream).

## Client obligations

- Render `token`s incrementally; clear on `retry`; show `citations`/`followups` on arrival.
- Treat `approval_required` as "waiting for a human", not as failure and not as completion.
- Switch on `error.code` **when present**; fall back to `content`. Do not assume a `code`.
- Tolerate unknown `type`s and unknown extra fields.

## Not in the vocabulary

The chat turn stream has no `paused`, `heartbeat` or per-token usage event (checked by grepping
every `"type": "…"` literal under `app/`). Usage is recorded server-side (the ledger), not
streamed. `started` and `progress` exist only on the separate **ingestion** job stream
(`GET /ingest/stream/{job_id}`), which has its own vocabulary and is out of scope here.
