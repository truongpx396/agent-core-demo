"""Alternative streaming implementation, kept for reference/comparison:
`_langfuse_trace`/`astream_events_turn_ctx` implement the same production
streaming contract as `runtime_stream.py`'s `astream_events_turn`, via
`@asynccontextmanager` for Langfuse's open/flush lifecycle instead of
manual trace bookkeeping. Split out of `runtime.py` for file size only
(see that module's docstring); `runtime_stream.py` is the sibling split
holding the actual production path.

Not wired into any production caller or test — `astream_events_turn` is
what every real caller (app/api/main.py, chat.py, agent_worker.py,
telegram.py) uses. Kept as a working illustration of the alternative
shape, not dead code to delete.

Reads `init_graph_async`/`_ensure_seeded_async`/`RECURSION_LIMIT` via
`runtime_module.X` rather than bare imports — same monkeypatch reasoning
as `runtime_stream.py`'s module docstring.
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
# vs. astream_events_turn's manual trace: open/flush is scoped to an
# `async with` block, composable with other async context managers, and
# easier to swap for a no-op in tests. Same event shapes either way —
# interchangeable from the caller's perspective.


@asynccontextmanager
async def _langfuse_trace(name: str, session_id: str, input_text: str):
    """Async context manager: opens a Langfuse trace, yields
    (trace_object | None, callbacks_list), and flushes on exit (normal or
    exception) so traces aren't lost on a fast return (tests, one-shot CLI
    runs).

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
    """Production async generator — Langfuse tracing via context manager
    instead of astream_events_turn's manual trace object.

    `ctx` is required (see stream_turn's docstring).

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

    # Opens the Langfuse trace and flushes automatically on exit.
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
