"""Production streaming path: per-turn metrics/timeout helpers, multimodal
content assembly (`_build_human_content`), Langfuse trace open
(`_open_trace`), the `astream_events` event-translation core
(`_run_graph_stream`), and the public streaming entry points
(`astream_events_turn`, `astream_events_turn_unattended`,
`astream_events_resume`, `astream_events_continue_turn`, `cancel_run`,
`get_session_messages`, `get_pending_approval`). Split out
of `app/agent/runtime.py` for file size only (see that module's
docstring); `runtime_legacy_stream.py` holds the sibling
`@asynccontextmanager` variant.

Reads `init_graph_async`/`_ensure_seeded_async`/`_upsert_session`/
`_tenant_over_daily_budget`/`_tenant_budget_envelope`/`RECURSION_LIMIT` via
`runtime_module.X` rather than bare imports: tests
`monkeypatch.setattr` these onto the live `app.agent.runtime` module
object, and a bare imported name would bind to the original function at
this module's own import time, permanently — the monkeypatch would
silently never take effect (same fix as `graph_agent_node.py`'s
`graph_module.CHAT_MODEL`). Applied uniformly even to names nothing
patches today, so it doesn't need a per-name judgment call later.

`_open_trace` stays a bare reference here: unlike the names above, both it
and its callers (`astream_events_turn`, `astream_events_resume`) live in
this same file, so it already resolves dynamically against this module's
own globals. Tests patch `app.agent.runtime_stream._open_trace` directly.
"""
import asyncio
import logging
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from app.agent import runtime as runtime_module
from app.agent.graph_compaction import COMPACTION_MARKER_KEY
from app.agent.graph_hitl import (
    CANCEL_SENTINEL,
    paused_approval_async,
    resumability_error_async,
)
from app.core import metrics, tracing
from app.core.config import (
    CHAT_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    UNATTENDED_MAX_DECLINE_ROUNDS,
)
from app.core.errors import (
    ErrorCode,
    ErrorEnvelope,
    TurnCancelled,
    internal_error_envelope,
)
from app.core.security import SecurityCtx

logger = logging.getLogger(__name__)

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None


# How long an unfinished turn waits for its last checkpoint before giving up on
# accounting for it. A turn ends on this path BECAUSE something was slow or
# broken, so the read is bounded: better an unrecorded turn than a terminal event
# held up behind the same outage.
UNFINISHED_TURN_STATE_READ_TIMEOUT_SECONDS = 2.0


async def _record_turn_metrics(
    elapsed: float,
    outcome: str,
    state: dict | None = None,
    ctx: SecurityCtx | None = None,
    thread_id: str | None = None,
    *,
    observe_iterations: bool = True,
) -> None:
    metrics.agent_requests_total.labels(outcome=outcome).inc()
    metrics.agent_latency_seconds.observe(elapsed)
    if state is not None:
        # `agent_iterations` means "how many round trips a TURN takes" (the
        # overview dashboard), so an unfinished turn leaves it alone.
        if observe_iterations:
            metrics.agent_iterations.observe(state.get("iterations", 0))
        total_tokens = state.get("total_tokens", 0)
        if total_tokens:
            metrics.agent_tokens_total.inc(total_tokens)
            # Usage ledger (pattern 26) — only recorded where ctx/thread_id
            # are actually available.
            # record_usage degrades to a no-op on its own failure.
            if ctx is not None and thread_id is not None:
                from app.agent import usage_ledger

                await usage_ledger.record_usage(
                    ctx, thread_id, CHAT_MODEL, total_tokens, state.get("total_cost_usd", 0.0)
                )


async def _record_unfinished_turn(graph, cfg, start: float, outcome: str) -> None:
    """Records a turn that ended by timeout, error or cancellation: the outcome
    and latency always, and the tokens it had already spent when its last
    checkpoint holds any.

    These branches used to pass no state at all, on the stated reasoning that
    they "have total_tokens == 0 anyway". They don't: steps the model had already
    completed are in the checkpoint, and the turns that run until the timeout are
    the ones most likely to have spent a lot — so they were the ones missing from
    the ledger the tenant's daily budget is checked against (spec 008, B17).
    Tokens of a model call cut off mid-flight never reached a checkpoint and are
    not counted. A turn paused for approval and never resumed also records
    nothing (disclosed in GRAPH_PATTERNS.md pattern 26).

    Never raises and never waits long: this runs while a turn is already failing,
    and accounting for it must not make that worse or hold up its terminal event.
    """
    values = None
    try:
        snapshot = await asyncio.wait_for(
            graph.aget_state(cfg), timeout=UNFINISHED_TURN_STATE_READ_TIMEOUT_SECONDS
        )
        values = dict(snapshot.values) if snapshot is not None and snapshot.values else None
    except Exception as exc:  # noqa: BLE001 - accounting must not worsen a turn that is already failing
        logger.warning("unfinished_turn_state_read_failed", extra={"error_class": type(exc).__name__})
    configurable = cfg.get("configurable") or {}
    try:
        await _record_turn_metrics(
            time.monotonic() - start,
            outcome,
            values,
            ctx=configurable.get("ctx"),
            thread_id=configurable.get("thread_id"),
            observe_iterations=False,
        )
    except Exception as exc:  # noqa: BLE001 - same: the turn's terminal event comes first
        logger.warning("unfinished_turn_record_failed", extra={"error_class": type(exc).__name__})


def _text_content(content) -> str:
    """LangChain message `.content` is either a plain str or a list of
    multimodal parts (GRAPH_PATTERNS.md pattern 44 — an attached image
    rides alongside text in the same list); this flattens either shape to
    plain text. Shared by _run_graph_stream's token-chunk handling and
    get_session_messages' transcript replay below, rather than
    duplicating the same normalization at each call site."""
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content


def _turn_outcome(state: dict) -> str:
    # validate_input resets iterations to 0 every turn; it only stays 0 if
    # the turn never reached `agent` at all, i.e. the reject_input path.
    return "rejected" if state.get("iterations", 0) == 0 else "success"


async def _iterate_with_timeout(aiter, timeout_seconds: float, cancel_check=None):
    """Wrap an async iterator so the whole run aborts once total wall-clock
    time exceeds `timeout_seconds` (enforces REQUEST_TIMEOUT_SECONDS).
    Raises TimeoutError on the next pending event; callers already wrap
    this loop in a generic `except Exception` for Langfuse error-marking.

    `cancel_check` (optional, `Callable[[], Awaitable[bool]]`) is polled
    once per iteration, before waiting on the next event; if it returns
    True, raises `TurnCancelled`. Cancellation only takes effect at the
    next event boundary, not mid-flight on an already-running tool/model
    call. Wired by `app/job_queue/agent_worker.py` to a Redis cancel flag
    (`POST /chat/cancel` → `queue.py::is_cancelled`); other callers pass
    nothing and keep timeout-only behavior.
    """
    aiter = aiter.__aiter__()
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Request exceeded {timeout_seconds}s timeout")
        if cancel_check is not None and await cancel_check():
            raise TurnCancelled()
        try:
            yield await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return


def _build_human_content(text: str, images: list[str] | None) -> str | list[str | dict]:
    """Plain `str` when there's no image, so a text-only turn is
    byte-identical to before this feature existed. With images, returns
    the multimodal `[{"type": "text", ...}, {"type": "image_url", ...}]`
    shape (pattern 44).

    `images` are data URIs or plain URLs; this app never fetches/decodes
    them or checks vision capability up front — that's on whatever model
    backs `CHAT_MODEL`.
    """
    if not images:
        return text
    parts: list[str | dict] = [{"type": "text", "text": text}]
    parts.extend({"type": "image_url", "image_url": {"url": img}} for img in images)
    return parts


# ---------------------------------------------------------------------------
# Production streaming via astream_events (v2)
# ---------------------------------------------------------------------------
# astream_events emits a granular event per lifecycle change (vs. polling
# full state snapshots), so the UI can show raw tokens
# (on_chat_model_stream) and per-tool spinners (on_tool_start/on_tool_end).
# Events are serialised to a unified SSE-safe dict so FastAPI/CLI don't
# need to know the LangGraph version.

def _open_trace(name: str, session_id: str, input_text: str):
    """Best-effort: open a Langfuse trace and a CallbackHandler scoped to it.

    Returns (trace_or_None, callbacks_list). Never raises — Langfuse is
    optional (see the CallbackHandler import at the top of this module).
    Shared by astream_events_turn and astream_events_resume so both open
    traces the same way (each gets its OWN trace rather than reusing one
    across a pause — see astream_events_resume's docstring for why).
    """
    callbacks = [metrics.MetricsCallbackHandler()]
    trace = None
    if CallbackHandler is not None:
        try:
            # The process-wide client (app/core/tracing.py), not a new one per
            # turn: each construction starts three threads nothing stops.
            lf = tracing.get_langfuse()
            if lf is not None:
                trace = lf.trace(name=name, session_id=session_id, input=input_text)
                # stateful_client (not trace_id) is CallbackHandler's actual
                # parameter for this in langfuse==2.60.10 — passing trace_id
                # raises TypeError, which used to be silently swallowed here,
                # leaving every graph node span unreported.
                callbacks.append(
                    CallbackHandler(stateful_client=trace, session_id=session_id)
                )
        except Exception:  # noqa: BLE001, S110 - Langfuse optional
            pass
    return trace, callbacks


async def _run_graph_stream(graph, graph_input, cfg, trace, cancel_check=None):
    """Shared core of astream_events_turn/astream_events_resume: drives one
    `graph.astream_events()` call, translates events into this app's typed
    event shapes, and yields exactly one terminal event:

      {"type": "approval_required", "tool_calls": [...]} — paused at
        human_approval's interrupt() (graph_hitl.py); resume via
        astream_events_resume(thread_id, approved).
      {"type": "retry"} — NOT terminal; tells the client to discard
        everything streamed so far because that content was just
        rejected/replaced (else old and retried answers render
        concatenated with no separator — a real bug this fixes). Fired
        from three `on_chain_end` sources, each replacing the last message
        in place:
          - `retry_output`: check_output rejected the answer; loops back
            to `agent`, which supplies fresh tokens normally.
          - `check_output` itself, when it mechanically inserted a missing
            citation marker after the answer already streamed — no next
            `agent` call is coming, so a synthetic "token" event carries
            the corrected text right after "retry".
          - `retry_exhausted`: route_after_check gave up after
            MAX_CONSECUTIVE_SAME_RETRY_REASON and replaces the message
            (same synthetic-token handling) — EXCEPT for
            `_TRUST_CONTENT_RETRY_REASONS` (e.g. a real answer just
            missing its citation marker), where it no-ops and the
            already-streamed content stands as the final answer; firing
            "retry" unconditionally there would blank a good answer with
            nothing to follow it.
      {"type": "compacted"} — NOT terminal; `compact_history` (pattern 41)
        already trimmed context before `agent` ran, so there's nothing to
        clear — purely informational (lets a UI show "summarizing older
        messages"). Only fires when it actually trimmed something; the
        node runs every turn but no-ops on most of them.
      {"type": "citations", "items": [...]} / {"type": "followups",
        "items": [...]} — emitted right before "done", when the answer
        cited something / suggest_followups (pattern 27) produced any.
      {"type": "done"} — the turn finished.
      {"type": "error", "content": "<message>"} — includes a
        user-initiated stop (see TurnCancelled below), modeled as a
        terminal envelope like any other error, not a separate SSE type
        (pattern 30).

    Handles Langfuse trace update/flush and turn metrics identically for
    both entry points. `cancel_check` (optional) forwards to
    `_iterate_with_timeout`; only `agent_worker.py`'s turn-job dispatch
    passes one today.
    """
    start = time.monotonic()
    final_answer = []
    used_citations: list[dict] = []
    ungrounded_claims_count = 0
    followups: list[str] = []
    try:
        async for event in _iterate_with_timeout(
            graph.astream_events(graph_input, config=cfg, version="v2"),
            REQUEST_TIMEOUT_SECONDS,
            cancel_check=cancel_check,
        ):
            kind = event["event"]

            if (
                kind == "on_chat_model_stream"
                and event.get("metadata", {}).get("langgraph_node") == "agent"
                and not event.get("metadata", {}).get("subagent_name")
            ):
                # astream_events emits on_chat_model_stream for EVERY
                # chat-model call in the graph, not just the main answer —
                # without this filter, suggest_followups (pattern 27) and
                # compact_history (pattern 41)'s own separate llm.invoke()
                # calls streamed as "token" events too, concatenating onto
                # the real answer with no separator (a real bug). `metadata.
                # langgraph_node == "agent"` isolates the main-answer node.
                #
                # `not subagent_name` guards a second source of the same
                # problem: tools.py::_run_subagent_impl threads this turn's
                # callbacks into a NESTED graph invoke() (built via the same
                # build_graph(), so it also has a node named "agent") for
                # correct child-span tracing — without this guard its
                # internal reasoning tokens would leak into the main answer
                # stream, indistinguishable from it. `parent_ids` can't
                # discriminate here (even top-level events nest 2+ levels
                # deep); `metadata.subagent_name`, stamped only on the
                # nested run's own config, can.
                content = _text_content(event["data"]["chunk"].content)
                if content:
                    final_answer.append(content)
                    yield {"type": "token", "content": content}

            elif kind == "on_tool_start":
                payload = {
                    "type": "tool_start",
                    "tool": event["name"],
                    "args": event["data"].get("input", {}),
                }
                subagent_name = event.get("metadata", {}).get("subagent_name")
                if subagent_name:
                    # Tool call happened inside a run_subagent's nested run
                    # (tagged via nested_config["metadata"]); tag the
                    # payload so the UI attributes it to the subagent rather
                    # than a top-level tool call.
                    payload["subagent"] = subagent_name
                yield payload

            elif kind == "on_tool_end":
                payload = {"type": "tool_end", "tool": event["name"]}
                subagent_name = event.get("metadata", {}).get("subagent_name")
                if subagent_name:
                    payload["subagent"] = subagent_name
                yield payload

            elif kind == "on_chain_end" and event["name"] == "retry_output":
                # check_output rejected the answer; discard what's streamed
                # so far (see this function's docstring, "retry").
                final_answer.clear()
                yield {"type": "retry"}

            elif kind == "on_chain_end" and event["name"] == "check_output":
                # check_output mechanically inserted a missing citation
                # marker after the answer already streamed (see docstring)
                # — only fires when it actually returned a replacement.
                output = event["data"].get("output") or {}
                replacement_messages = output.get("messages") or []
                if replacement_messages:
                    final_answer.clear()
                    yield {"type": "retry"}
                    replacement_text = replacement_messages[-1].content
                    if isinstance(replacement_text, str) and replacement_text:
                        final_answer.append(replacement_text)
                        yield {"type": "token", "content": replacement_text}

            elif kind == "on_chain_end" and event["name"] == "retry_exhausted":
                # route_after_check gave up after
                # MAX_CONSECUTIVE_SAME_RETRY_REASON and unconditionally
                # replaced the message — except for
                # _TRUST_CONTENT_RETRY_REASONS, where it no-ops (see this
                # function's docstring, "retry", for the two real bugs this
                # branch guards against).
                output = event["data"].get("output") or {}
                replacement_messages = output.get("messages") or []
                if replacement_messages:
                    final_answer.clear()
                    yield {"type": "retry"}
                    replacement_text = replacement_messages[-1].content
                    if isinstance(replacement_text, str) and replacement_text:
                        final_answer.append(replacement_text)
                        yield {"type": "token", "content": replacement_text}

            elif kind == "on_chain_end" and event["name"] == "compact_history":
                # Runs every turn but only does real work once its
                # ceiling trips; `output` is `{}` (graph.py's early
                # return) otherwise.
                output = event["data"].get("output") or {}
                if output.get("messages"):
                    yield {"type": "compacted"}

    except TurnCancelled:
        # Checked before the generic except below — a deliberate stop gets
        # its own ErrorCode rather than ErrorCode.INTERNAL.
        if trace:
            trace.update(
                output="".join(final_answer) + " [cancelled by user]", level="WARNING"
            )
        await _record_unfinished_turn(graph, cfg, start, "cancelled")
        metrics.agent_streaming_cancellation_total.inc()
        envelope = ErrorEnvelope(code=ErrorCode.CANCELLED, message="Cancelled by user.")
        terminal_event = {"type": "error", "content": envelope.message, **envelope.to_dict()}
    except asyncio.CancelledError:
        # Real asyncio task cancellation (ASGI teardown on disconnect, an
        # outer wait_for timing out, etc.) — distinct from TurnCancelled's
        # cooperative Redis-flag poll. CancelledError is a BaseException
        # (Python 3.8+) specifically so `except Exception` below can't
        # swallow it; without this branch a real cancellation skipped both
        # except clauses and the generator died with no clean terminal
        # event or metrics (only Langfuse's own CallbackHandler recorded
        # anything, leaving spans open with raw asyncio internals as the
        # status message). Re-raises after cleanup so the caller (e.g.
        # agent_worker.py's dispatch loop) still sees it.
        if trace:
            trace.update(
                output="".join(final_answer) + " [cancelled: task cancelled]",
                level="WARNING",
            )
        await _record_unfinished_turn(graph, cfg, start, "cancelled")
        metrics.agent_streaming_cancellation_total.inc()
        raise
    except Exception as exc:  # noqa: BLE001
        if trace:
            trace.update(output=f"error: {exc}", level="ERROR")
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        await _record_unfinished_turn(graph, cfg, start, outcome)
        # The caller never sees `str(exc)`: an unexpected exception's text
        # can name an internal host, a SQL fragment or a DSN, and this event
        # crosses the trust boundary (Principle V, pattern 30). The full
        # text is on the trace above; the log line below carries the class
        # only, matching the rest of the repo.
        logger.warning("graph_stream_failed", extra={"error_class": type(exc).__name__})
        if outcome == "timeout":
            # asyncio.wait_for's own internal timeout raises a bare
            # TimeoutError with an empty str(exc), so the message is spelled
            # out rather than taken from the exception.
            envelope = ErrorEnvelope(
                code=ErrorCode.TIMEOUT,
                message=f"Request exceeded {REQUEST_TIMEOUT_SECONDS}s timeout",
            )
        else:
            envelope = internal_error_envelope(exc)
        # "content" kept alongside the envelope fields for backward
        # compatibility with existing consumers (chat.py, the web UI) that
        # already read event["content"] — see errors.py's docstring.
        terminal_event = {"type": "error", "content": envelope.message, **envelope.to_dict()}
    else:
        # astream_events() just stops yielding when the run pauses at an
        # interrupt() — no exception, no distinct "paused" event — so
        # check state.next to tell "paused" from "finished". aget_state,
        # not get_state: this runs directly on the checkpointer's own
        # event loop (via init_graph_async()), where only the async
        # accessor is safe (see resumability_error_async's docstring).
        state = await graph.aget_state(cfg)
        if state.next:
            pending = state.tasks[0].interrupts[0].value
            if trace:
                trace.update(
                    output="".join(final_answer) + " [paused: awaiting approval]"
                )
            terminal_event = {
                "type": "approval_required",
                "tool_calls": pending["tool_calls"],
            }
        else:
            used_citations = state.values.get("used_citations") or []
            ungrounded_claims_count = state.values.get("ungrounded_claims_count") or 0
            followups = state.values.get("followups") or []
            if not final_answer:
                # Nodes that produce a final AIMessage without calling the
                # chat model (reject_input/reject_context/
                # reject_moderation, context_window_exceeded, a
                # semantic-cache hit (pattern 22), or the no_answer safety
                # net) never fire on_chat_model_stream, so final_answer
                # would otherwise be empty and the caller gets only a bare
                # "done" with no answer text (a real bug). Sent as a
                # synthetic "token" event so existing clients render it
                # unchanged.
                final_message = state.values["messages"][-1]
                skipped_text = (
                    final_message.content if isinstance(final_message.content, str) else ""
                )
                if skipped_text:
                    # Append to final_answer too, not just yield — trace.update
                    # below reads final_answer, so skipping this left the
                    # Langfuse trace blank despite the client getting the
                    # real text (a second real bug).
                    final_answer.append(skipped_text)
                    yield {"type": "token", "content": skipped_text}
            if trace:
                trace.update(output="".join(final_answer))
            await _record_turn_metrics(
                time.monotonic() - start,
                _turn_outcome(state.values),
                state.values,
                ctx=(cfg.get("configurable") or {}).get("ctx"),
                thread_id=(cfg.get("configurable") or {}).get("thread_id"),
            )
            terminal_event = {"type": "done"}
    # No per-turn flush: the shared client's own background consumer sends
    # events as they arrive and app/core/tracing.py flushes it at process exit.
    # The flush that used to live in a `finally` here built a NEW client, whose
    # queue was empty, so it neither flushed this trace nor was free — and a
    # real flush blocks until the queue drains, which would stall the event loop.

    if used_citations or ungrounded_claims_count:
        yield {
            "type": "citations",
            "items": used_citations,
            "ungrounded_claims_count": ungrounded_claims_count,
        }
    if followups:
        # suggest_followups (pattern 27) computes these into
        # state["followups"], but this streaming path never surfaced them
        # until now — the web UI already has CSS for follow-up chips
        # (index.html's .followups) with no event feeding it.
        yield {"type": "followups", "items": followups}
    yield terminal_event


async def astream_events_turn(
    text: str,
    thread_id: str,
    ctx: SecurityCtx,
    require_approval: bool = False,
    images: list[str] | None = None,
    cancel_check=None,
):
    """Production async generator — yields typed event dicts (see
    _run_graph_stream for the full shape list).

    `ctx` is required (see stream_turn's docstring). `require_approval`
    mirrors graph.py's opt-in HITL gate (should_continue): a tool call
    pauses instead of executing, and the caller resumes via
    astream_events_resume(thread_id, approved, ctx) — see
    app/channels/chat.py's `--hitl` mode. `images` (pattern 44) is
    optional. `cancel_check` forwards to `_run_graph_stream`; only
    agent_worker.py's turn-job dispatch passes one (wired to a Redis flag
    via `POST /chat/cancel`).

    Refused up front (an `error` event, never reaching the graph) if this
    tenant's rolling 24h spend already reached
    MAX_COST_USD_PER_TENANT_PER_DAY, or if UNPRICED_MODEL_POLICY is "block" and
    the chat model has no known price (nothing could be metered).
    """
    if await runtime_module._chat_model_refused_as_unpriced():
        metrics.agent_requests_total.labels(outcome="rejected").inc()
        envelope = runtime_module._model_unpriced_envelope()
        yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
        return
    if await runtime_module._tenant_over_daily_budget(ctx):
        metrics.agent_requests_total.labels(outcome="rejected").inc()
        envelope = runtime_module._tenant_budget_envelope()
        yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
        return
    # Reserves this turn's worst-case cost against the tenant's in-flight
    # total for the rest of this generator's life (released in `finally`
    # below, unconditionally) — see runtime.py::_reserve_turn_budget's own
    # docstring for the race this closes: without it, a sibling turn for
    # the same tenant starting moments later would see the exact same
    # `spent` this turn's own check just read, since usage_ledger only
    # gets this turn's real cost once it's done.
    budget_hold = await runtime_module._reserve_turn_budget(ctx)
    try:
        graph = await runtime_module.init_graph_async()
        pending = await paused_approval_async(graph, {"configurable": {"thread_id": thread_id}})
        if pending is not None:
            # A new turn arriving while this thread is still paused at
            # human_approval — "double texting" onto an approval gate
            # (LangGraph Platform's own term for a new message mid-run; the
            # open-source library leaves handling it to the app, see
            # docs.langchain.com/langsmith/double-texting).
            if pending["resumable"]:
                # Refuse rather than silently cancel: a pending tool_call here
                # is, by construction, non-read_only (TOOL_CAPABILITIES) — an
                # AUTO-cancel on the caller's behalf could discard a mutating
                # action a caller who didn't know it was pending (a second
                # tab, Telegram, a bare API call, a race before the web UI's
                # own session-switcher reshow resolves) never chose to drop.
                # The web UI's own composer already disables itself while
                # paused (`!activeTurn` guard) and re-shows this exact pause
                # on return (GET .../pending_approval), so a caller that goes
                # through it never reaches this branch at all; this is the
                # explicit refusal for everything else.
                envelope = ErrorEnvelope(
                    code=ErrorCode.PENDING_APPROVAL,
                    message="This conversation has a pending approval — approve, reject, or cancel it before sending a new message.",
                    details={"tool_calls": pending["tool_calls"]},
                )
                yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
                return
            # checkpoint_incompatible (schema/topology changed since this
            # thread paused) — refusing here would strand the thread
            # permanently: cancel_run refuses for the identical reason
            # (Command(resume=...) needs to locate the interrupted task in
            # the CURRENT graph object, same as resuming would), so there is
            # no action any caller could take to unblock it. Proceeding is
            # the only path that makes progress; surfaced rather than silent.
            yield {
                "type": "system_note",
                "content": "A previous pending approval on this conversation could not be resumed after an app update; starting a new request.",
            }
        await runtime_module._ensure_seeded_async(graph, thread_id)
        await runtime_module._upsert_session(ctx, thread_id, text)
        trace, callbacks = _open_trace("chat-turn-stream", thread_id, text)
        cfg = {
            "configurable": {"thread_id": thread_id, "ctx": ctx},
            "callbacks": callbacks,
            "recursion_limit": runtime_module.RECURSION_LIMIT,
        }
        graph_input = {
            "messages": [HumanMessage(content=_build_human_content(text, images))],
            "require_approval": require_approval,
        }
        async for event in _run_graph_stream(graph, graph_input, cfg, trace, cancel_check=cancel_check):
            yield event
    finally:
        _release_turn_budget_in_background(ctx, budget_hold)


def _release_turn_budget_in_background(ctx: SecurityCtx | None, budget_hold: str | None) -> None:
    """Releases a turn's budget hold without awaiting it.

    Fire-and-forget, NOT awaited: the generator's caller
    (agent_worker.py::_process_turn) only releases its own per-thread lock
    (queue.py::acquire_thread_lock) once THAT generator is fully exhausted —
    awaiting a real Postgres round trip in its `finally` would sit between
    "terminal event already published to the results stream" and "thread lock
    released," a window a caller reacting immediately to that terminal event
    (e.g. POST /chat/resume fired the instant a client sees approval_required)
    can land in, getting spuriously rejected as THREAD_BUSY even though the turn
    it's resuming already finished from its own point of view. Reproduced
    directly: tests/integration/test_worker_scaling.py's real-subprocess HITL
    test started failing intermittently once this await was added.

    Safe to detach: _release_turn_budget already fails open internally (never
    raises in practice), and a reservation that takes a few extra milliseconds to
    clear is harmless — it's not on any correctness-critical path, only
    in_flight_reservation's own read, which also ignores a hold older than
    RESERVATION_STALE_AFTER_MINUTES. Shared by every entry point that reserves
    (a new turn, a resume, a crash-continue) so they cannot drift apart.
    """
    release_task = asyncio.create_task(runtime_module._release_turn_budget(ctx, budget_hold))
    release_task.add_done_callback(_log_if_release_task_failed)


def _log_if_release_task_failed(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "tenant_budget_release_task_failed", extra={"error_class": type(exc).__name__}
        )


async def astream_events_turn_unattended(
    text: str,
    thread_id: str,
    ctx: SecurityCtx,
    require_approval: bool = False,
    images: list[str] | None = None,
):
    """astream_events_turn, but auto-declines an approval_required pause
    instead of yielding it — for callers with no interactive human on this
    end of the call: telegram.py (no inline-keyboard approve/reject UX) and
    any fire-and-forget caller (pattern 43). Never approves.

    The decline is a LOOP, bounded by `UNATTENDED_MAX_DECLINE_ROUNDS`, not a
    single round. Real bug (fixed): after the first decline the model can
    re-request the same gated write, which pauses the conversation again. A
    single-round version forwarded that second `approval_required` and left
    the checkpoint paused; an unattended channel cannot resolve a pause, so
    every later message was refused with "pending approval" and the first
    message got an empty reply (GRAPH_PATTERNS.md pattern 8). Each decline
    counts `agent_unattended_pause_total`.

    If the model is still asking once the ceiling is reached, the run is
    CANCELLED (`cancel_run`) so the conversation is never left paused, and one
    explicit assistant-visible message says the action needs a person's
    approval and was not done. Exactly one terminal event is yielded either
    way. Fails closed throughout: a declined or cancelled action never runs.
    """
    paused = False
    pending_tools: list[str] = []

    async def _drain(stream):
        """Forward every event except a pause, which is held back and recorded
        in `paused`/`pending_tools` (a separate flag: a pause is a pause even
        if its tool list were somehow empty)."""
        nonlocal paused
        paused = False
        pending_tools.clear()
        async for event in stream:
            if event.get("type") == "approval_required":
                paused = True
                pending_tools.extend(tc.get("name", "?") for tc in event.get("tool_calls", []))
                continue
            yield event

    async for event in _drain(
        astream_events_turn(text, thread_id, ctx, require_approval=require_approval, images=images)
    ):
        yield event

    declines = 0
    while paused:
        if declines >= UNATTENDED_MAX_DECLINE_ROUNDS:
            names = ", ".join(sorted(set(pending_tools))) or "an action"
            await cancel_run(thread_id, ctx)
            yield {
                "type": "token",
                "content": (
                    f"That needs a person's approval ({names}), which this channel can't provide — "
                    "so it wasn't done."
                ),
            }
            yield {"type": "done"}
            return
        declines += 1
        metrics.agent_unattended_pause_total.inc()
        # admitted=True: this decline is a step of the request that already passed the
        # allowance at its start. Re-checking here could refuse it and leave the
        # conversation paused — the stranded-pause bug this loop exists to prevent.
        async for event in _drain(astream_events_resume(thread_id, False, ctx, admitted=True)):
            yield event


async def astream_events_resume(
    thread_id: str, approved: bool, ctx: SecurityCtx, cancel_check=None, *, admitted: bool = False
):
    """Resume a turn paused by astream_events_turn(require_approval=True) —
    streaming counterpart to `graph.invoke(Command(resume=approved), config)`.
    `thread_id` must match the paused turn.

    `cancel_check` (optional) forwards to `_run_graph_stream`, same contract
    as `astream_events_turn`'s: without it, a `POST /chat/cancel` after the
    user approved a tool set a flag nothing polled, so the approved write,
    the model call that reads its result and any further tool rounds all ran
    to the end. It is polled before the first event, so a cancel that lands
    between the approval and the worker picking the job up stops the turn
    *before* the approved tool runs; one that lands later stops it at the
    next event boundary (an already-running tool call finishes — see
    `_iterate_with_timeout`). Callers with no cancel path
    (`astream_events_turn_unattended`, chat.py) pass nothing.

    `ctx` is required and re-supplied here rather than reused from the
    original pause: `config["configurable"]` does not persist across a
    resume (verified empirically), so whoever resolves the approval must
    re-assert identity. (A stricter check — e.g. resuming principal must
    match the pausing tenant — would go here too; today
    `resumability_error_async` only checks checkpoint build compatibility,
    not identity.)

    Opens its own Langfuse trace (correlated by session_id) rather than
    extending the original, since the approval wait can be arbitrarily
    long — also why `resumability_error_async` is checked here: this is
    the entry point most likely to hit a stale/incompatible checkpoint in
    practice.

    The allowance is checked here like a new turn's (spec 008 A6, FR-022): the
    approval wait is unbounded, so by the time someone approves, the tenant may
    be well past its ceiling — and a resume goes on to run the approved tool and
    then call the model again. The hold the paused turn took was released when its
    generator ended at the pause, so a resume also takes its own, and N paused
    turns resuming at once are visible to each other. A refused resume leaves the
    thread paused (the caller can cancel it: `cancel_run` is never gated, since it
    gives the model no turn); it does not discard the pending action.

    `admitted=True` skips the check for a resume that is a step of a request which
    already passed it — `astream_events_turn_unattended`'s decline loop. Refusing
    there would strand the conversation at its pause.
    """
    if not admitted and await runtime_module._tenant_over_daily_budget(ctx):
        metrics.agent_requests_total.labels(outcome="rejected").inc()
        envelope = runtime_module._tenant_budget_envelope()
        yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
        return
    budget_hold = await runtime_module._reserve_turn_budget(ctx)
    try:
        graph = await runtime_module.init_graph_async()
        error = await resumability_error_async(graph, {"configurable": {"thread_id": thread_id}})
        if error:
            yield {"type": "error", "content": error}
            yield {"type": "done"}
            return

        trace, callbacks = _open_trace(
            "chat-turn-stream-resume", thread_id, f"resume(approved={approved})"
        )
        cfg = {
            "configurable": {"thread_id": thread_id, "ctx": ctx},
            "callbacks": callbacks,
            "recursion_limit": runtime_module.RECURSION_LIMIT,
        }
        async for event in _run_graph_stream(
            graph, Command(resume=approved), cfg, trace, cancel_check=cancel_check
        ):
            yield event
    finally:
        _release_turn_budget_in_background(ctx, budget_hold)


async def astream_events_continue_turn(thread_id: str, ctx: SecurityCtx, cancel_check=None):
    """Continues a `"turn"` that already started (its `HumanMessage` is
    checkpointed) but crashed before finishing — streaming counterpart to
    `graph.astream_events(None, config)`. The ONLY caller is
    `agent_worker.py`'s reclaim path, for a `"turn"` job
    `_classify_reclaimed_turn` proved is genuinely mid-flight: not paused
    at `human_approval` (that's `astream_events_resume`'s job), not
    already finished (nothing to continue).

    Why this is safe even if the crashed attempt already ran a
    mutating/outward tool call, unconditionally, with no per-tool
    capability check needed: passing `None` as input (vs a fresh
    `{"messages": [...]}` dict) tells LangGraph's Pregel loop to set
    `is_resuming=True` and proceed from wherever THIS checkpoint's own
    unfinished superstep left off, matching any already-recorded task
    write (verified directly against the installed `langgraph==0.2.76`'s
    `pregel/loop.py::Loop._match_writes`, fed from
    `checkpoint_pending_writes` — persisted per-task in Postgres by
    `AsyncPostgresSaver`, not just at full-superstep boundaries) instead
    of re-executing it. A tool call that already completed is never run
    twice; the loop just moves on to whatever comes after it. This is the
    exact mechanism that already makes `astream_events_resume` safe to
    blindly retry, extended here to an ordinary crashed turn.

    Contrast with re-issuing a brand-new `astream_events_turn` call
    instead (what a naive crash-recovery retry would do): passing REAL
    input hits `Loop._first`'s OTHER branch, which explicitly discards any
    unfinished-task writes from the previous checkpoint and starts a fresh
    superstep sequence from the graph's entry edges — a brand-new `agent`
    call with brand-new `tool_call_id`s unrelated to the crashed attempt's
    own. `app/agent/tool_idempotency.py` dedupes by `tool_call_id`, so it
    can't catch a duplicate there — which is exactly why restarting a turn
    that already ran a mutating tool used to be treated as unsafe
    (dead-lettered) rather than retried. Continuing instead of restarting
    closes that gap structurally rather than by scanning for which tools
    are dangerous to repeat.

    No `_ensure_seeded_async`/`_upsert_session` here, deliberately: this thread
    is already mid-turn, not starting a new one.

    The allowance is NOT re-checked here, unlike `astream_events_resume`
    (spec 008 A6): this is a retry of work that was already admitted, and
    refusing it could strand a turn that already ran a mutating tool — the
    exact state this path exists to finish rather than abandon. The overshoot
    that costs is bounded by that one turn's own `MAX_COST_USD_PER_TURN`. What
    it does take is a hold: the crashed worker's was lost with it, so without
    one this turn's remaining spend is invisible to its siblings' checks.
    """
    budget_hold = await runtime_module._reserve_turn_budget(ctx)
    try:
        async for event in _continue_turn_events(thread_id, ctx, cancel_check):
            yield event
    finally:
        _release_turn_budget_in_background(ctx, budget_hold)


async def _continue_turn_events(thread_id: str, ctx: SecurityCtx, cancel_check):
    """The body of `astream_events_continue_turn`, split out so the hold above
    wraps every exit path of it with one try/finally."""
    graph = await runtime_module.init_graph_async()
    cfg_probe = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(cfg_probe)
    if state.next and any(task.interrupts for task in state.tasks):
        # The crashed attempt reached human_approval's interrupt() but
        # died before its own ack (`process_request` acks a job the
        # instant its generator finishes — reaching a real pause counts —
        # so a "turn" job only ever gets reclaimed if the crash happened
        # strictly before that point; this is the sliver where it happened
        # strictly after). Feeding a bare `None` into an interrupted task
        # supplies no resume value — that path belongs to
        # `astream_events_resume`'s `Command(resume=...)`, not this one.
        envelope = ErrorEnvelope(
            code=ErrorCode.PENDING_APPROVAL,
            message="This turn paused for approval right before a worker crash — resume or cancel it instead of retrying.",
        )
        yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
        return

    trace, callbacks = _open_trace("chat-turn-stream-continue", thread_id, "continue(crash-recovery)")
    cfg = {
        "configurable": {"thread_id": thread_id, "ctx": ctx},
        "callbacks": callbacks,
        "recursion_limit": runtime_module.RECURSION_LIMIT,
    }
    async for event in _run_graph_stream(graph, None, cfg, trace, cancel_check=cancel_check):
        yield event


async def cancel_run(thread_id: str, ctx: SecurityCtx) -> bool:
    """Cancel a run paused at human_approval (pattern 36) by resuming with
    `CANCEL_SENTINEL` — a third outcome distinct from True/False:
    `human_approval` treats it as an abort where the gated action never
    runs and the model never gets a turn to react (a caller-initiated
    stop, not feedback for another attempt). Because it spends nothing it is
    deliberately NOT subject to the tenant allowance: an over-budget tenant must
    always be able to clear a paused conversation.

    Returns `True` if a paused run was cancelled, `False` if there was
    nothing to cancel (a stale/missing checkpoint per
    `resumability_error_async` — not an error worth raising).
    """
    graph = await runtime_module.init_graph_async()
    error = await resumability_error_async(graph, {"configurable": {"thread_id": thread_id}})
    if error:
        return False
    cfg = {"configurable": {"thread_id": thread_id, "ctx": ctx}, "recursion_limit": runtime_module.RECURSION_LIMIT}
    await graph.ainvoke(Command(resume=CANCEL_SENTINEL), config=cfg)
    metrics.agent_cancellation_total.inc()
    return True


async def get_session_messages(thread_id: str) -> list[dict]:
    """Session-switcher transcript replay
    (`GET /chat/sessions/{thread_id}/messages`, app/api/routers/chat.py) — reads
    the same Postgres checkpointer every resume/cancel path uses
    (sessions.py's `chat_sessions` table only has title/timestamps, not
    content).

    No `ctx` here on purpose: a thread_id's checkpoint carries no owner to
    check against. Ownership (does this thread_id belong to the caller's
    tenant+principal) is the CALLER's job, via
    app/agent/sessions.py::list_sessions, before calling this.

    Returns `[{"role": "user"|"assistant"|"system", "text": str}, ...]` —
    a replay of what the user actually saw, not a full forensic trace
    (that's Langfuse): `SystemMessage`s and `ToolMessage`s are omitted, and
    a tool-calling `AIMessage` with no text of its own is skipped. One
    exception: a compact_history breadcrumb (tagged via
    `COMPACTION_MARKER_KEY`) comes back as `role: "system"` so a replay
    shows that older turns were cut, not just silently missing.
    """
    graph = await runtime_module.init_graph_async()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    messages = []
    for m in state.values.get("messages", []):
        if isinstance(m, HumanMessage):
            role = "user"
        elif isinstance(m, AIMessage):
            role = "assistant"
        elif isinstance(m, SystemMessage) and m.additional_kwargs.get(COMPACTION_MARKER_KEY):
            role = "system"
        else:
            continue
        text = _text_content(m.content)
        if text:
            messages.append({"role": role, "text": text})
    return messages


async def get_pending_approval(thread_id: str) -> dict | None:
    """Session-switcher pause check
    (`GET /chat/sessions/{thread_id}/pending_approval`, app/api/routers/chat.py) —
    lets the web UI re-show the approve/reject banner when the user
    returns to a thread that's still paused at human_approval, instead of
    looking idle (its own `activeTurn` is in-memory JS state a page
    reload/session switch never repopulates; `get_session_messages` above
    already skips a tool-calling `AIMessage` with no text, so a paused
    turn otherwise renders as if nothing happened after the user's last
    message).

    No `ctx`, same reasoning as `get_session_messages`. Returns `None` if
    not paused, else `{"tool_calls": [...], "resumable": bool}` —
    `resumable=False` means `state_schema_version` no longer matches this
    build (see `resumability_error_async`'s docstring); the caller should
    show that as unresumable rather than a working Approve button.
    """
    graph = await runtime_module.init_graph_async()
    return await paused_approval_async(graph, {"configurable": {"thread_id": thread_id}})
