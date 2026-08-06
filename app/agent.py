"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the two entry points `stream_turn` (CLI, streaming) and
`answer` (API, single response). Keeping this in one place means memory and
tracing behave identically no matter which front-end is used.

Production streaming is also provided via `astream_events_turn` — see below.
"""
import json
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe

from app.graph import SYSTEM_PROMPT, build_graph

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_seeded: set[str] = set()


def get_graph():
    """Compile the LangGraph agent once and reuse it (shares MemorySaver)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _callbacks(session_id: str):
    if CallbackHandler is None:
        return []
    try:
        return [CallbackHandler(session_id=session_id)]
    except Exception:  # noqa: BLE001
        return []


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
    printed = ""
    for state in graph.stream(
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
        stream_mode="values",
    ):
        msg = state["messages"][-1]
        if msg.type == "ai" and isinstance(msg.content, str) and msg.content:
            delta = msg.content[len(printed):]
            printed = msg.content
            yield delta


@observe(name="chat-turn")
def answer(text: str, thread_id: str) -> str:
    """Run one turn and return the final assistant text (used by the API)."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    result = graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
    )
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
    callbacks = []
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
            callbacks = [CallbackHandler(
                trace_id=trace.id,
                session_id=thread_id,
            )]
        except Exception:  # noqa: BLE001 - Langfuse optional
            pass

    cfg = {
        "configurable": {"thread_id": thread_id},
        "callbacks": callbacks,
        "recursion_limit": 12,
    }

    final_answer = []
    try:
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=text)]},
            config=cfg,
            version="v2",
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
        yield {"type": "error", "content": str(exc)}
    else:
        if trace:
            trace.update(output="".join(final_answer))
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
    callbacks: list = []
    if CallbackHandler is not None:
        try:
            from langfuse import Langfuse
            lf = Langfuse()
            trace = lf.trace(name=name, session_id=session_id, input=input_text)
            callbacks = [CallbackHandler(trace_id=trace.id, session_id=session_id)]
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

        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content=text)]},
                config=cfg,
                version="v2",
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
            yield {"type": "error", "content": str(exc)}
            return  # exit before the else-branch and done event

        if trace:
            trace.update(output="".join(final_answer))

    yield {"type": "done"}
