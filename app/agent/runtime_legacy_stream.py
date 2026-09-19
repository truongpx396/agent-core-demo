"""Alternative streaming implementation kept for reference/comparison —
`_langfuse_trace`/`astream_events_turn_ctx` demonstrate the SAME
production streaming contract (`app/agent/runtime_stream.py`'s
`astream_events_turn`) expressed via an `@asynccontextmanager` for
Langfuse's open/flush lifecycle instead of manual trace bookkeeping. Split
out of `app/agent/runtime.py` purely for file size — see that module's own
docstring and `app/agent/runtime_stream.py` for the sibling split (the
actual production path every real caller uses).

Not wired into any production caller or test today — `astream_events_turn`
(runtime_stream.py) is what `app/api/main.py`/`app/channels/chat.py`/
`app/job_queue/agent_worker.py`/`app/channels/telegram.py` all actually
call. Kept as a working, runnable illustration of the alternative
context-manager shape (see its own module comment for the tradeoffs), not
dead weight to delete outright — no behavior change from the pre-split
single-file version either way.

Reads `init_graph_async`/`_ensure_seeded_async`/`RECURSION_LIMIT` through
`runtime_module.X` rather than plain statically-imported bare names — same
reasoning as `app/agent/runtime_stream.py`'s own module docstring.
"""
import time
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage

from app.agent import runtime as runtime_module
from app.agent.runtime_stream import (
    CallbackHandler,
    _iterate_with_timeout,
    _record_turn_metrics,
    _text_content,
    _turn_outcome,
)
from app.core import metrics
from app.core.config import REQUEST_TIMEOUT_SECONDS
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx

# ---------------------------------------------------------------------------
# Alternative: Langfuse via @asynccontextmanager
# ---------------------------------------------------------------------------
# Key difference from astream_events_turn (manual trace):
#   - Langfuse resource lifecycle (open → flush) is expressed as an
#     `async with` block via `@asynccontextmanager`, keeping acquisition and
#     cleanup co-located in one place.
#   - Reusable: `_langfuse_trace()` can be composed with other async context
#     managers (e.g. httpx sessions, DB transactions) at the call site.
#   - Easier to test: swap in a no-op context manager in unit tests without
#     touching the streaming logic.
#
# Both versions yield the same event dict shapes — they are interchangeable
# from the caller's perspective (CLI, tests).


@asynccontextmanager
async def _langfuse_trace(name: str, session_id: str, input_text: str):
    """Async context manager that opens a Langfuse trace and flushes on exit.

    Yields (trace_object | None, callbacks_list) to the caller.
    On exit — whether by normal return or exception — flushes the Langfuse
    queue so traces aren't lost when the process returns quickly (e.g. tests,
    one-shot CLI invocations).

    Usage::
        async with _langfuse_trace("my-trace", thread_id, text) as (trace, cbs):
            cfg = {"callbacks": cbs, ...}
            # ... do work ...
            if trace:
                trace.update(output="final answer")
    """
    trace = None
    callbacks: list = [metrics.MetricsCallbackHandler()]
    if CallbackHandler is not None:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            trace = lf.trace(name=name, session_id=session_id, input=input_text)
            # stateful_client, not trace_id — see the matching note in
            # astream_events_turn.
            callbacks.append(
                CallbackHandler(stateful_client=trace, session_id=session_id)
            )
        except Exception:  # noqa: BLE001, S110 — Langfuse optional
            pass
    try:
        yield trace, callbacks
    finally:
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:  # noqa: BLE001, S110 - best-effort flush on the way out
                pass


async def astream_events_turn_ctx(text: str, thread_id: str, ctx: SecurityCtx):
    """Production async generator — Langfuse tracing via context-manager.

    Uses `_langfuse_trace` (an `@asynccontextmanager`) so trace open/flush
    lifecycle is scoped to the `async with` block.  Compare with the sibling
    `astream_events_turn` which manages the trace object manually.

    `ctx` is required — see stream_turn's matching docstring note.

    Yields the same event shapes as `astream_events_turn`:
      {"type": "token",      "content": "<text chunk>"}
      {"type": "tool_start", "tool": "<name>", "args": {…}}
      {"type": "tool_end",   "tool": "<name>"}
      {"type": "done"}
      {"type": "error",      "content": "<message>"}
    """
    graph = await runtime_module.init_graph_async()
    await runtime_module._ensure_seeded_async(graph, thread_id)

    final_answer: list[str] = []

    # `async with` opens the Langfuse trace and hands us the callbacks list.
    # On exit (normal or exception) the context manager flushes automatically.
    async with _langfuse_trace("chat-turn-stream-ctx", thread_id, text) as (
        trace,
        callbacks,
    ):
        cfg = {
            "configurable": {"thread_id": thread_id, "ctx": ctx},
            "callbacks": callbacks,
            "recursion_limit": runtime_module.RECURSION_LIMIT,
        }

        start = time.monotonic()
        try:
            async for event in _iterate_with_timeout(
                graph.astream_events(
                    {"messages": [HumanMessage(content=text)]},
                    config=cfg,
                    version="v2",
                ),
                REQUEST_TIMEOUT_SECONDS,
            ):
                kind = event["event"]

                if kind == "on_chat_model_stream":
                    content = _text_content(event["data"]["chunk"].content)
                    if content:
                        final_answer.append(content)
                        yield {"type": "token", "content": content}

                elif kind == "on_tool_start":
                    yield {
                        "type": "tool_start",
                        "tool": event["name"],
                        "args": event["data"].get("input", {}),
                    }

                elif kind == "on_tool_end":
                    yield {"type": "tool_end", "tool": event["name"]}

        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.update(output=f"error: {exc}", level="ERROR")
            outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
            await _record_turn_metrics(time.monotonic() - start, outcome)
            # See _run_graph_stream's matching comment: a bare
            # asyncio.wait_for timeout has an empty str(exc).
            message = (
                f"Request exceeded {REQUEST_TIMEOUT_SECONDS}s timeout"
                if outcome == "timeout"
                else str(exc)
            )
            envelope = ErrorEnvelope(
                code=ErrorCode.TIMEOUT if outcome == "timeout" else ErrorCode.INTERNAL,
                message=message,
            )
            yield {"type": "error", "content": message, **envelope.to_dict()}
            return  # exit before the else-branch and done event

        if trace:
            trace.update(output="".join(final_answer))
        # aget_state, not get_state — see _run_graph_stream's matching
        # comment: this runs on the checkpointer's own event loop.
        final_state = (await graph.aget_state(cfg)).values
        await _record_turn_metrics(
            time.monotonic() - start, _turn_outcome(final_state), final_state
        )

    yield {"type": "done"}
