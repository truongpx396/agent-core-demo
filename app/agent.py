"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the two entry points `stream_turn` (CLI, streaming) and
`answer` (API, single response). Keeping this in one place means memory and
tracing behave identically no matter which front-end is used.

Production streaming is also provided via `astream_events_turn` — see below.
"""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe

from app import metrics
from app.graph import SYSTEM_PROMPT, build_graph

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_seeded: set[str] = set()

# Safety budget: bound total wall-clock time per turn, beneath which
# MAX_ITERATIONS (LLM loop cap) and recursion_limit (graph step cap, in
# _config() below) already apply. This is the outermost layer — a turn
# stuck inside a single slow LLM/tool call never reaches those.
REQUEST_TIMEOUT_SECONDS = 60
# Soft-timeout executor for the sync `answer()` path (used by POST /chat):
# Python can't forcibly kill a running thread, so this bounds how long the
# *caller* waits, not how long graph.invoke() keeps running in the
# background after that.
_REQUEST_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="request-timeout")


def _record_turn_metrics(elapsed: float, outcome: str, state: dict | None = None) -> None:
    metrics.agent_requests_total.labels(outcome=outcome).inc()
    metrics.agent_latency_seconds.observe(elapsed)
    if state is not None:
        metrics.agent_iterations.observe(state.get("iterations", 0))
        total_tokens = state.get("total_tokens", 0)
        if total_tokens:
            metrics.agent_tokens_total.inc(total_tokens)


def _turn_outcome(state: dict) -> str:
    # validate_input resets iterations to 0 every turn; it only stays 0 if
    # the turn never reached `agent` at all, i.e. the reject_input path.
    return "rejected" if state.get("iterations", 0) == 0 else "success"


async def _iterate_with_timeout(aiter, timeout_seconds: float):
    """Wrap an async iterator so the whole run aborts if total wall-clock
    time exceeds `timeout_seconds` — enforces REQUEST_TIMEOUT_SECONDS for
    the astream_events paths. Raises TimeoutError on the next pending
    event; callers already have a generic `except Exception` around this
    loop (for Langfuse error-marking), so no separate handling is needed.
    """
    aiter = aiter.__aiter__()
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Request exceeded {timeout_seconds}s timeout")
        try:
            yield await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return


def get_graph():
    """Compile the LangGraph agent once and reuse it (shares MemorySaver)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _callbacks(session_id: str):
    callbacks = [metrics.MetricsCallbackHandler()]
    if CallbackHandler is not None:
        try:
            callbacks.append(CallbackHandler(session_id=session_id))
        except Exception:  # noqa: BLE001
            pass
    return callbacks


def _ensure_seeded(graph, thread_id: str) -> None:
    """Seed a new conversation thread with the system prompt exactly once."""
    if thread_id in _seeded:
        return
    graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": [SystemMessage(content=SYSTEM_PROMPT)]},
    )
    _seeded.add(thread_id)


def _config(thread_id: str) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "callbacks": _callbacks(thread_id),
        "recursion_limit": 12,  # safety net so a weak model can't loop forever
    }


@observe(name="chat-turn-stream")
def stream_turn(text: str, thread_id: str):
    """Yield the assistant's answer incrementally (used by the CLI)."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    start = time.monotonic()
    deadline = start + REQUEST_TIMEOUT_SECONDS
    printed = ""
    final_state: dict = {}
    for state in graph.stream(
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
        stream_mode="values",
    ):
        final_state = state
        if time.monotonic() > deadline:
            # Soft timeout: checked between graph steps, not preemptive —
            # a single slow step can still finish before this is reached.
            yield "\n[stopped: request exceeded timeout]"
            _record_turn_metrics(time.monotonic() - start, "timeout", final_state)
            return
        msg = state["messages"][-1]
        if msg.type == "ai" and isinstance(msg.content, str) and msg.content:
            delta = msg.content[len(printed):]
            printed = msg.content
            yield delta
    _record_turn_metrics(
        time.monotonic() - start, _turn_outcome(final_state), final_state
    )


@observe(name="chat-turn")
def answer(text: str, thread_id: str) -> str:
    """Run one turn and return the final assistant text (used by the API)."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)

    start = time.monotonic()
    future = _REQUEST_EXECUTOR.submit(
        graph.invoke,
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
    )
    try:
        result = future.result(timeout=REQUEST_TIMEOUT_SECONDS)
    except FuturesTimeoutError:
        _record_turn_metrics(time.monotonic() - start, "timeout")
        return "Sorry, that took too long to answer — please try again."
    except Exception:
        _record_turn_metrics(time.monotonic() - start, "error")
        raise

    _record_turn_metrics(time.monotonic() - start, _turn_outcome(result), result)
    return result["messages"][-1].content


# ---------------------------------------------------------------------------
# Production streaming via astream_events (v2)
# ---------------------------------------------------------------------------
# This is the industry-standard approach for production agents. Instead of
# polling full state snapshots (`stream_mode="values"` + string slicing),
# `astream_events` emits a granular event per lifecycle change so the UI can:
#   - Show raw tokens instantly   → on_chat_model_stream
#   - Show a spinner per tool     → on_tool_start / on_tool_end
# All events are serialised to a unified SSE-safe dict so the FastAPI layer
# can forward them verbatim and the CLI can render them without knowing
# which LangGraph version generated them.

async def astream_events_turn(text: str, thread_id: str):
    """Production async generator — yields typed event dicts.

    Event shapes (all JSON-serialisable):
      {"type": "token",      "content": "<text chunk>"}
      {"type": "tool_start", "tool": "<name>", "args": {…}}
      {"type": "tool_end",   "tool": "<name>"}
      {"type": "done"}
      {"type": "error",      "content": "<message>"}

    Langfuse tracing: because @observe cannot wrap async generators, we open
    a trace manually and pass it to the LangChain CallbackHandler so every
    nested LLM call and tool call is attached to the same trace in Langfuse.
    """
    graph = get_graph()
    _ensure_seeded(graph, thread_id)

    # --- Langfuse: open a trace manually (works with async generators) ---
    trace = None
    callbacks = [metrics.MetricsCallbackHandler()]
    if CallbackHandler is not None:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            trace = lf.trace(
                name="chat-turn-stream",
                session_id=thread_id,
                input=text,
            )
            # Attach a callback handler scoped to this trace so all nested
            # LLM calls and tool calls appear as children in Langfuse.
            callbacks.append(CallbackHandler(
                trace_id=trace.id,
                session_id=thread_id,
            ))
        except Exception:  # noqa: BLE001 - Langfuse optional
            pass

    cfg = {
        "configurable": {"thread_id": thread_id},
        "callbacks": callbacks,
        "recursion_limit": 12,
    }

    start = time.monotonic()
    final_answer = []
    try:
        async for event in _iterate_with_timeout(
            graph.astream_events(
                {"messages": [HumanMessage(content=text)], "require_approval": True},
                config=cfg,
                version="v2",
            ),
            REQUEST_TIMEOUT_SECONDS,
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
                # Raw token chunk — content may be str or list (multimodal)
                chunk = event["data"]["chunk"]
                content = chunk.content
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in content
                    )
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
        _record_turn_metrics(time.monotonic() - start, outcome)
        yield {"type": "error", "content": str(exc)}
    else:
        if trace:
            trace.update(output="".join(final_answer))
        final_state = graph.get_state(cfg).values
        _record_turn_metrics(
            time.monotonic() - start, _turn_outcome(final_state), final_state
        )
    finally:
        # Flush so the trace is sent even if the caller exits immediately.
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:  # noqa: BLE001
                pass

    yield {"type": "done"}


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
# from the caller's perspective (FastAPI /chat/stream, CLI, tests).


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
            callbacks.append(CallbackHandler(trace_id=trace.id, session_id=session_id))
        except Exception:  # noqa: BLE001 — Langfuse optional
            pass
    try:
        yield trace, callbacks
    finally:
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:  # noqa: BLE001
                pass


async def astream_events_turn_ctx(text: str, thread_id: str):
    """Production async generator — Langfuse tracing via context-manager.

    Uses `_langfuse_trace` (an `@asynccontextmanager`) so trace open/flush
    lifecycle is scoped to the `async with` block.  Compare with the sibling
    `astream_events_turn` which manages the trace object manually.

    Yields the same event shapes as `astream_events_turn`:
      {"type": "token",      "content": "<text chunk>"}
      {"type": "tool_start", "tool": "<name>", "args": {…}}
      {"type": "tool_end",   "tool": "<name>"}
      {"type": "done"}
      {"type": "error",      "content": "<message>"}
    """
    graph = get_graph()
    _ensure_seeded(graph, thread_id)

    final_answer: list[str] = []

    # `async with` opens the Langfuse trace and hands us the callbacks list.
    # On exit (normal or exception) the context manager flushes automatically.
    async with _langfuse_trace("chat-turn-stream-ctx", thread_id, text) as (
        trace,
        callbacks,
    ):
        cfg = {
            "configurable": {"thread_id": thread_id},
            "callbacks": callbacks,
            "recursion_limit": 12,
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
                    chunk = event["data"]["chunk"]
                    content = chunk.content
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content
                        )
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
            _record_turn_metrics(time.monotonic() - start, outcome)
            yield {"type": "error", "content": str(exc)}
            return  # exit before the else-branch and done event

        if trace:
            trace.update(output="".join(final_answer))
        final_state = graph.get_state(cfg).values
        _record_turn_metrics(
            time.monotonic() - start, _turn_outcome(final_state), final_state
        )

    yield {"type": "done"}
