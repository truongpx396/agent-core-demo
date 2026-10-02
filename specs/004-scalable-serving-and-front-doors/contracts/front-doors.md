# Contract: Front Doors (web page, terminal)

**Feature**: [spec.md](../spec.md) | **HTTP**: feature 001 [chat-turn-http.md](../../001-core-rag-agent-turn/contracts/chat-turn-http.md)
| **Chat app**: [chat-app-channel.md](./chat-app-channel.md)

**Status**: Retrospective — `app/api/static/index.html`, `app/channels/chat.py`. The page has no hermetic behavioral test
(A7); the terminal has none at all (A2).

## What every door must do

The obligations are feature 001's *Client obligations* (`turn-event-stream.md`): render tokens incrementally, clear on
`retry`, show `citations` and `followups` on arrival, treat `approval_required` as "waiting for a human", switch on
`error.code` when present, tolerate unknown event types. This table records which door meets what.

| Obligation | Web page | Terminal | Chat app |
|------------|----------|----------|----------|
| render `token` incrementally | ✔ draft area, promoted when confirmed | ✔ printed inline | collected into one reply |
| clear on `retry` | ✔ discards the draft only | **✘ (B6)** | **✘ (B6)** |
| show `citations` | ✔ | **✘ not rendered (A2)** | ✔ `Sources:` list |
| show `followups` | ✔ | **✘ (A2)** | not applicable |
| tool activity | ✔ | ✔ `[▶ tool(args)]` / `[✓ tool done]` | not shown |
| `approval_required` | ✔ approve/reject buttons | ✔ `Approve? [y/N]` | declined (unattended) |
| stop a running turn | ✔ Stop → `POST /chat/cancel` | none — `Ctrl-C` at the prompt ends the session; during a turn it is not handled | not available |
| unknown event types | ignored | ignored | ignored |

## Web page

- One self-contained HTML file at `GET /`; talks only to the published endpoints; `fetch()` with a hand-rolled SSE
  parser (`EventSource` cannot POST or send headers).
- Sends on every request: `X-Tenant-Id`, `X-Principal-Id` (from selectors, defaults `ecorp` / `web-user`) and
  `X-Domain` (from the domain selector). Upload requests use the same identity headers without a JSON content type.
- **A change of tenant, principal or domain starts a fresh conversation** (new `thread_id`, cleared transcript, switcher
  re-fetched) — a conversation belongs to the identity and domain that started it.
- A non-2xx response (a 422, a 429, a 404) makes `pumpSSE` throw `HTTP <status>`, which the turn shows as `[error: Error: HTTP <status>]` unless
  text had already streamed — the response body's `detail` (e.g. the 429 text) is **not** shown for chat requests. An in-stream
  `error` event is rendered as the turn's result, keeping any partial text already streamed.
- A reopened session re-fetches its transcript and `pending_approval`; a pause is made actionable again.
- Escapes HTML and validates link targets before rendering model-produced text.
- Persists only preferences in `localStorage` (data-model §5); every access is guarded.
- The selectors are editable by design: it is a demo of the trusted-header seam, **not** authentication (feature 002).

## Terminal

- `python -m app.channels.chat [--hitl]` (`make chat`, `make chat-hitl`). Initializes the checkpointer on its loop, then
  loops `you> ` → one turn.
- Identity: tenant `DEFAULT_TENANT`, principal `local:<os user>`. A fresh `thread_id` per session.
- Runs `astream_events_turn(require_approval=hitl)` and, **whenever the run pauses** (every mutating or outward call,
  with or without `--hitl`), prints the pending calls and asks `Approve? [y/N]`; only `y` approves; it then resumes with
  `astream_events_resume` and repeats while the run keeps pausing.
- `exit`, `quit`, EOF or Ctrl-C ends the session and flushes the trace buffer.
- Errors are printed in red as `[error: <content>]`.
