# Contract: Logs, the Correlation Id and Traces

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §5–§6](../data-model.md) | **Constitution**: Principle V (last bullet), Principle VI

**Status**: Retrospective — `app/core/logging_config.py`, `app/core/metrics.py::MetricsCallbackHandler`/`_fingerprint`, `app/agent/graph_utils.py::_instrumented`, `app/agent/runtime_stream.py::_open_trace`/`_run_graph_stream`, `app/agent/runtime_legacy_stream.py`;
tests in `tests/core/test_logging_config.py`, `tests/core/test_metrics.py` (`TestToolCallAuditLog`), `tests/agent/test_streaming_terminal_events.py` (`TestTraceOutputMatchesWhatTheClientActuallySaw`).

## Audience

An **operator** searching logs and traces, and an **engineer** writing a log line or opening a trace.

## Logs

- **Which processes**: every long-running service (API, agent worker, ingest worker, channels). **Not** the interactive CLI or one-shot scripts, which print for a person.
- **Shape**: one JSON object per line — `timestamp`, `level`, `logger`, `message`, `request_id` (when bound), the call's `extra` fields, `exc_info` as a traceback **string** (data-model §5). A non-JSON-native extra is stringified, never fatal.
- **Correlation**: `bind_request_id(id)` wraps one turn or job; every line emitted inside — however deep, in code that never heard of the id — carries it; the id is reset on exit including on error; an id supplied explicitly in `extra` wins.
- **Node lifecycle**: every graph node is wrapped at registration by `_instrumented` — `node_started`, then `node_completed` (with `duration_ms`), `node_failed` or `node_paused` (a human-approval pause is **not** a failure and is re-raised untouched).
- **Per-tool audit** (`MetricsCallbackHandler`): `tool_called`, `tool_succeeded`, `tool_failed` with `run_id`, a 16-hex **fingerprint** of arguments and result, and the error **class** — never the text.
- **What a log line MUST NOT contain**: message text, document text, tool arguments or results, the `state` dict, an exception's *message* (its class only; `graph_stream_failed` logs `error_class`).
- **Shipping**: Promtail → Loki, 168 h retention.

## Traces (optional)

- **When**: `_open_trace` runs for each new turn and each resume (each gets its own trace — a trace does not span a pause). It builds a Langfuse client, opens a trace with the **user's text as input** and attaches a callback handler that records **every LLM prompt and completion and every tool input and output**; the stream core later writes the **final answer** (or `[paused: awaiting approval]`, `[cancelled by user]`, `error: <text>`) as the trace output and flushes in a `finally`.
- **Best-effort**: any failure opening a trace is swallowed; no keys means a *disabled* client — the turn is unaffected.
- **Scrubbing**: tool results are credential-scrubbed before they reach a trace; the user's text and the final answer are **not**.
- **Content**: **yes, by design** — a trace exists so an operator can see what the model saw and said. This is *not* "metadata only" and is the subject of **A10** (the constitution's Principle V says otherwise).
- **Resource cost**: a **new client per turn, plus another per flush**; each starts three background threads that never exit (**B18**), traced or not.

## Choosing where information goes

| You want to record… | Put it in | Not in |
|---------------------|-----------|--------|
| That something happened, how often | a counter (closed labels) | a log line that someone must grep |
| That something happened to *this* turn | a log line with `run_id`/`request_id` and an error class | the message, args or result text |
| What the model saw and said | the trace (access-controlled) | logs or metric labels |
| What a call returned, for "was it the same as before" | the 16-hex fingerprint | the value |

## Invariants a change must preserve

1. A log line never carries message text, tool arguments or results, or an exception's text.
2. Every line inside a turn or job carries its correlation id.
3. A pause is a pause, not a failure, in logs and traces.
4. Telemetry never fails a turn and never grows the process per turn *(not yet true — B18)*.
5. If traces are to be content-free, that is a change to what the callback records, decided through the constitution process **(A10)**.
