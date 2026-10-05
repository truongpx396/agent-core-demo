---
paths:
  - "app/agent/graph*.py"
  - "app/agent/runtime*.py"
  - "app/job_queue/**/*.py"
  - "app/ingestion/ingest_queue.py"
  - "app/ingestion/ingest_worker.py"
  - "app/api/main.py"
  - "app/api/deps.py"
  - "app/api/routers/**/*.py"
  - "app/core/metrics.py"
  - "app/core/resilience.py"
  - "deploy/litellm/patches/**"
  - "observability/**"
---

# Graph, runtime, queue and observability gotchas

Constitution Principles V and VI apply. Each item below exists because it cost a real debugging
session; `GRAPH_PATTERNS.md` has the full story under the pattern number given.

## Graph (`graph*.py`)

- `SYSTEM_PROMPT` stays a ctx-free constant (pattern 19). Never interpolate a tenant, principal,
  or timestamp; `tests/agent/test_prompt_cache_stability.py` guards it.
- Nodes are wrapped by `_instrumented` in `build_graph` at registration time, not in the node
  body. `human_approval`'s `interrupt()` is a pause (`node_paused`), not a failure — re-raise it.
  Never log message content or the `state` dict.
- Anything that rejects or skips pending tool calls (HITL rejection, over-budget batch) MUST emit
  one `ToolMessage` per pending `tool_call`, or the next LLM call fails (patterns 8, 10).
- History trimming is turn-aware: never split a `tool_call` from its `ToolMessage` (pattern 13).
- `validate_input` resets `iterations`/`total_tokens` every turn but NOT on a HITL resume.
- A breaking `State` / topology change bumps `STATE_SCHEMA_VERSION`; a differing build SHA alone
  is not a break (`resumability_error_async`, pattern 16).
- `build_graph` and `build_subagent_graph` share `_assemble_shared_graph_parts`. A new node must
  be considered for both; the subagent graph deliberately omits cache/follow-up/compaction nodes.
- Every graph caller is async. `AsyncPostgresSaver`'s lock is bound to the loop that opened it:
  open it through `init_graph_async` on the calling loop. `MemorySaver` is for tests/subagents.
- Don't reorder the bound tool list: `skill_search`/`use_skill` sit first on purpose (pattern 50).
- Parallel tool calls are supported. The old glued-together `tool_calls` corruption was a LiteLLM
  chunk-index bug, fixed in `deploy/litellm/patches/sitecustomize.py` (loaded via `PYTHONPATH` in
  docker-compose) — not by `parallel_tool_calls=False`, which was a no-op and has been removed.
  Treat that patch file as load-bearing.

## Queue and workers (`job_queue/`, `ingest_*`)

- Delivery is at-least-once: ACK only after `process_request` finishes, so every handler MUST be
  safe under redelivery.
- Reclaim never restarts blindly. A crashed `"turn"` is judged by `_classify_reclaimed_turn` and
  continued via `astream_events_continue_turn`, because a restart re-asks the LLM and mints new
  `tool_call_id`s.
- One requests stream per domain (`requests_stream_key`). Domain is inferred from the stream a
  job landed on, never from its payload; an unknown `X-Domain` is a 422.
- A blocking `XREAD` needs `socket_timeout=None` on the redis-py async client (its 5s default
  races the server-side `BLOCK`).
- Every wait for a counterparty has a deadline (`read_results(first_event_deadline_seconds=...)`),
  and a claim that wins but whose publish fails is compensated (`release_submission_claim`).

## Resilience (`core/resilience.py`)

- `CircuitBreaker.call` retries only exceptions the caller names in `retry_on` and can justify as
  "never landed". Don't wrap a raw sandbox command or any non-idempotent write in it. Half-open
  admits exactly one trial under the lock.

## Metrics and alerts

- Add counters/histograms in `app/core/metrics.py` using its `.labels(...).inc()` surface;
  histograms get explicit buckets, not SDK defaults. Telemetry is configured at process start
  (`configure_telemetry`), never at import time.
- Every degrade-and-continue path increments a metric. If the failure can leave a human unaware of
  committed business state (failed notify, degraded dedup, failed upload), also add an alert
  rule to `observability/prometheus/alerts.yml` — a metric nobody alerts on is still silent.
- Caller-facing errors use the `ErrorCode` envelope (`app/core/errors.py`); a tool's message to
  the LLM stays natural language (`_friendly_tool_error`).
