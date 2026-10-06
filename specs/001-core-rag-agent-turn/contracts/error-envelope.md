# Contract: Error Envelope

**Feature**: [spec.md](../spec.md) | **Stream**: [turn-event-stream.md](./turn-event-stream.md)

**Status**: Retrospective — `app/core/errors.py` plus an audit of **every** place the code emits a
`{"type": "error", …}` event. The registry is a closed set; **the envelope is not applied
universally**, and this document records exactly where it is and is not.

## Shape

```json
{ "type": "error", "content": "<message>", "code": "<ErrorCode>", "message": "<message>", "details": {} }
```

`content` is kept alongside the envelope fields for older consumers (CLI, web UI) that read
`event["content"]`; `message` repeats it. `details` is `null` or an object (e.g.
`{"tool_calls": [...]}` for `pending_approval`). Produced by
`{"type": "error", "content": envelope.message, **envelope.to_dict()}`; `to_dict()` turns the
`code` enum into its plain string value so the object is JSON-safe with no custom encoder.

Applies to operator/caller-facing surfaces only. A failing tool's message **to the model** is
natural language by design and never uses this shape.

## Code registry (`ErrorCode`) and where each is actually emitted

| `code` | Emitted? | Emitter (as built) | Meaning |
|--------|----------|--------------------|---------|
| `timeout` | yes | `runtime_stream.py` (`_run_graph_stream`, generic `except` when the exception is a `TimeoutError`) | Whole-turn wall-clock limit exceeded. |
| `cancelled` | yes | `runtime_stream.py` (`TurnCancelled`), `agent_worker.py::_process_cancel` | A user-initiated stop. Modeled as a terminal `error`, not a separate event type. |
| `internal` | yes | `runtime_stream.py` (generic `except`), `runtime_legacy_stream.py` | Unexpected failure. `message` is a fixed text and `details.error_class` names the exception class — never `str(exc)` (fixed in #64, via `internal_error_envelope` in `app/core/errors.py`). |
| `pending_approval` | yes | `runtime_stream.py::astream_events_turn` and `::astream_events_continue_turn` | A new message arrived while the conversation is paused at an approval; or a crashed turn that had reached the pause. (Feature 003.) |
| `tenant_budget_exceeded` | yes | `budgets.py::refusal_envelope`, via `astream_events_turn` and `astream_events_resume` | The tenant's rolling-24 h spend reached its daily cap, or (when `MAX_COST_USD_PER_TENANT_PER_MONTH` is set) its calendar-month spend reached the monthly cap; refused before any model work. `details` carries `scope`, `window` and, for a month, `resets_at`. |
| `personal_budget_exceeded` | yes | `budgets.py::refusal_envelope`, via `astream_events_turn` and `astream_events_resume` | THIS PERSON's spend inside their tenant reached `MAX_COST_USD_PER_PRINCIPAL_PER_DAY` or `_PER_MONTH` (both off by default). Other people in the tenant are unaffected. `details` as above. |
| `budget_check_unavailable` | yes | `budgets.py::refusal_envelope`, via `astream_events_turn` and `astream_events_resume` | `BUDGET_CHECK_FAILURE_POLICY=closed` and the ledger could not be read, so the allowance could not be verified; refused before any model work. Retry shortly. |
| `model_unpriced` | yes | `runtime.py::_model_unpriced_envelope`, via `astream_events_turn` | `UNPRICED_MODEL_POLICY=block` and the chat model has no known price in LiteLLM, so no dollar ceiling could meter the turn; refused before any model work. An operator fix, not a caller one. |
| `thread_busy` | yes | `agent_worker.py::process_request` | Another job is already running on this conversation. |
| `worker_lost` | yes | `agent_worker.py::_handle_reclaimed_job` | The worker handling the request died and the job could not be safely retried. |
| `checkpoint_lost` | **no** | — | See deviation 2. |
| `checkpoint_incompatible` | **no** | — | See deviation 2. |
| `moderation_blocked` | **no** | — | A blocked message is delivered as assistant text + `done`. |
| `cost_ceiling_exceeded` | **no** | — | A tripped ceiling is delivered as assistant text + `done`. |
| `no_progress` | **no** | — | Same. |
| `unattended_pause` | **no** | — | An unattended caller's auto-declined pause is a counter (`agent_unattended_pause_total`), not an event. |

## Known deviations (disclosed; none are fixed by this spec batch)

1. **Six registered codes are never emitted** (`moderation_blocked`, `cost_ceiling_exceeded`,
   `no_progress`, `unattended_pause`, `checkpoint_lost`, `checkpoint_incompatible`). For the first
   four the outcome is deliberately an assistant answer, not an error, so a client cannot
   distinguish them from an ordinary reply except by counters.
2. **A refused resume is not enveloped.** `astream_events_resume` yields
   `{"type": "error", "content": "checkpoint_lost: …"}` (or `checkpoint_incompatible: …`) followed
   by `{"type": "done"}` — the code name appears only as a text prefix, and a `done` follows the
   `error` (the one place a stream has a terminal event *after* a terminal event; clients that stop
   at the first terminal event are unaffected).
3. **First-event deadline** (`queue.py::read_results`) yields
   `{"type": "error", "content": "No response for '<id>' after 30s — is an agent-worker running for this domain?"}`
   with no `code`.
4. ~~**Worker catch-all** forwarded `str(exc)` with no `code`~~ — **fixed in #64**: it now emits the `internal` envelope (fixed message, `details.error_class`), as do the stream core and the legacy stream (formerly item 5). The *ingest* worker's catch-all still forwards `str(exc)` (feature 006, B10).

Constitution Principle V requires caller-facing errors to use the envelope; items 2–3 are the literal
deviations still tracked as plan item **A2**.

## Consumer guidance

- Branch on `code` when present; otherwise on `content`. Do not require `code`.
- Treat an `error` whose `code` is absent as non-retryable-by-default unless its text identifies
  a known transient (`No response for …`).
- `thread_busy` and `worker_lost` are safe to retry after a pause; `pending_approval` is not (the
  user must approve/reject/cancel first); `tenant_budget_exceeded` and `personal_budget_exceeded` are not (until the window rolls; a monthly one says when in `details.resets_at`); `model_unpriced` is not (until an operator prices the model); `budget_check_unavailable` is (once the ledger answers again).
