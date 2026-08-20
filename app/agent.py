"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the two entry points `stream_turn` (CLI, streaming) and
`answer` (API, single response). Keeping this in one place means memory and
tracing behave identically no matter which front-end is used.

Production streaming is also provided via `astream_events_turn` — see below.

## Durable checkpointing (init_graph_sync / init_graph_async)

The graph is built with an `AsyncSqliteSaver` (survives a process restart),
not `build_graph()`'s bare-call default `MemorySaver` (gone the moment the
process exits) — a paused human_approval gate is only a meaningful safety
control if the pause actually survives a redeploy while someone reviews it.

Why two init functions instead of one: `AsyncSqliteSaver`'s async
lock/state is bound to whichever asyncio event loop it was created on.
Its SYNC methods (used by `graph.invoke`/`graph.stream`, i.e.
answer()/stream_turn()) work correctly when called from any *other*
thread than that loop — that's the documented, supported cross-thread
path. Its ASYNC methods (used by `graph.ainvoke`/`graph.astream_events`,
i.e. astream_events_turn/_resume) do NOT: calling them from a different
loop than the one they were created on raises "bound to a different event
loop" (asyncio locks are loop-bound, verified empirically before writing
this). So which loop the checkpointer is opened on matters:

- `init_graph_async()` — for a process with an asyncio loop it keeps
  alive for its whole lifetime (FastAPI's lifespan, running on uvicorn's
  loop; the CLI's `--stream` mode, running inside `asyncio.run()`). Opens
  the checkpointer directly on the CALLING (current) loop. Async graph
  calls then run natively on the same loop the checkpointer is bound to;
  sync calls (e.g. FastAPI's `/chat` running a plain `def` in its own
  threadpool) still work via the cross-thread path since that's a
  different thread than the loop's.
- `init_graph_sync()` — for a process with NO event loop of its own (the
  CLI's plain, non-`--stream` mode, and app/hitl_demo.py). Spins up one
  small background thread hosting a persistent event loop purely to give
  the checkpointer somewhere to live; sync graph calls reach it from any
  other thread exactly as above. This process path never calls the async
  graph methods, so the background loop only ever needs to service
  sync-dispatched calls — the constraint above never bites.

`get_graph()` falls back to `init_graph_sync()` if nothing has initialized
the singleton yet, so it stays a safe drop-in for any purely-sync caller.
An async entry point (astream_events_turn/_resume) MUST have had
`init_graph_async()` awaited first by its own process's startup path (see
app/api.py's lifespan, app/chat.py's async_main) — calling it after a sync
path already initialized the singleton would try to reuse a
background-thread-bound checkpointer from a different loop and hit the
same "bound to a different event loop" failure this design exists to avoid.
"""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app import metrics
from app.config import CHECKPOINT_DB_PATH
from app.graph import build_graph, resumability_error
from app.security import SecurityCtx

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_checkpointer_cm = None  # keeps AsyncSqliteSaver's context manager alive —
# from_conn_string is an @asynccontextmanager; letting the returned object
# get garbage-collected closes the connection out from under the saver
# (verified empirically: the very next call fails "no active connection").
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


async def _open_checkpointer():
    """Open the AsyncSqliteSaver on whichever loop calls this — see this
    module's docstring for why the calling loop matters. Returns the saver;
    stashes the context manager in the module global so it isn't
    garbage-collected out from under the connection."""
    global _checkpointer_cm
    cm = AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB_PATH)
    saver = await cm.__aenter__()
    await saver.setup()
    _checkpointer_cm = cm
    return saver


async def init_graph_async():
    """Initialize (or reuse) the shared graph for a process with its own
    persistent event loop (FastAPI's lifespan; the CLI's --stream mode) —
    see this module's docstring. Returns the graph. A no-op if the
    singleton already exists — both astream_events_turn/_resume call this
    directly (not get_graph()) precisely so the checkpointer ends up bound
    to whichever loop is *actually* driving them, self-healing even if a
    process's startup path forgot to prime it via lifespan; when startup
    DID prime it already, this is just a cheap existence check."""
    global _graph
    if _graph is None:
        saver = await _open_checkpointer()
        _graph = build_graph(checkpointer=saver)
    return _graph


def init_graph_sync() -> None:
    """Initialize the shared graph for a process with no event loop of its
    own (the CLI's plain mode; app/hitl_demo.py) — see this module's
    docstring. Starts one small daemon thread hosting a persistent loop
    purely so AsyncSqliteSaver has somewhere to live; that thread is never
    explicitly stopped (a daemon thread dies with the process, and SQLite's
    own journal makes an abrupt kill safe — same as a real deployment being
    SIGKILLed)."""
    global _graph
    if _graph is not None:
        return

    ready = threading.Event()
    holder: dict = {}

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            holder["saver"] = loop.run_until_complete(_open_checkpointer())
        except Exception as exc:  # noqa: BLE001 - surfaced to the waiting caller below
            holder["error"] = exc
            ready.set()
            return  # nothing will ever use this loop — don't run it forever
        ready.set()
        loop.run_forever()

    threading.Thread(
        target=_run_loop, daemon=True, name="checkpointer-loop"
    ).start()
    if not ready.wait(timeout=10):
        raise TimeoutError("Timed out starting the checkpointer's background loop")
    if "error" in holder:
        raise holder["error"]

    _graph = build_graph(checkpointer=holder["saver"])


def get_graph():
    """Return the shared graph, initializing it via init_graph_sync() if no
    entry point has primed it yet — a safe drop-in for any purely-sync
    caller. An async entry point must call `await init_graph_async()`
    itself first (see this module's docstring for why the order matters)."""
    if _graph is None:
        init_graph_sync()
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
    """Seed a new conversation thread with the system prompt exactly once.

    Reads `graph.manifest.system_prompt` (stamped by build_graph — see
    GRAPH_PATTERNS.md pattern 23, app/manifest.py) rather than the
    module-level `SYSTEM_PROMPT` constant, so this seeds the CORRECT
    prompt for whichever domain `graph` was actually built for — every
    graph build_graph() returns always carries a `.manifest`, defaulting
    to the Acme domain, so `graph.manifest.system_prompt` and the
    top-level `SYSTEM_PROMPT` import are identical for every caller in
    this app today (get_graph()/init_graph_sync/init_graph_async never
    pass a non-default manifest); this only starts to matter the day some
    caller does.
    """
    if thread_id in _seeded:
        return
    graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": [SystemMessage(content=graph.manifest.system_prompt)]},
    )
    _seeded.add(thread_id)


def _config(thread_id: str, ctx: SecurityCtx) -> dict:
    return {
        "configurable": {"thread_id": thread_id, "ctx": ctx},
        "callbacks": _callbacks(thread_id),
        "recursion_limit": 12,  # safety net so a weak model can't loop forever
    }


def _delta_for(state: dict, printed: str) -> str | None:
    """The new suffix of the latest AI message's content since `printed`,
    or None if this state snapshot has nothing new to show (a tool_call
    step, a non-AI message, etc.) — shared by stream_turn's initial pass
    and its auto-decline resume pass below."""
    msg = state["messages"][-1]
    if msg.type == "ai" and isinstance(msg.content, str) and msg.content:
        return msg.content[len(printed):]
    return None


@observe(name="chat-turn-stream")
def stream_turn(text: str, thread_id: str, ctx: SecurityCtx):
    """Yield the assistant's answer incrementally (used by the CLI).

    `ctx` is required, not optional-with-a-default: a caller with no valid
    tenant/principal to stamp shouldn't silently get a default identity
    (see app/security.py's SecurityCtx docstring) — graph.py's
    validate_input/route_after_validation fail closed on it regardless,
    but making it a required parameter here means that failure is visible
    at the call site, not just three nodes deep in the graph.
    """
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    start = time.monotonic()
    deadline = start + REQUEST_TIMEOUT_SECONDS
    printed = ""
    final_state: dict = {}
    cfg = _config(thread_id, ctx)

    for state in graph.stream(
        {"messages": [HumanMessage(content=text)]}, config=cfg, stream_mode="values"
    ):
        final_state = state
        if time.monotonic() > deadline:
            # Soft timeout: checked between graph steps, not preemptive —
            # a single slow step can still finish before this is reached.
            yield "\n[stopped: request exceeded timeout]"
            _record_turn_metrics(time.monotonic() - start, "timeout", final_state)
            return
        delta = _delta_for(state, printed)
        if delta:
            printed += delta
            yield delta

    if graph.get_state(cfg).next:
        # Paused at human_approval's mandatory capability gate (see
        # graph.py's should_continue) — this plain CLI mode is one-way
        # streaming with no approve/reject prompt, unlike `make
        # chat-stream`'s astream_events_turn/astream_events_resume round
        # trip. Auto-decline: never silently run an unreviewed
        # mutating/outward tool call, and never leave the thread paused
        # forever with nobody able to resume it.
        metrics.agent_unattended_pause_total.inc()
        error = resumability_error(graph, cfg)
        if error:  # never resume blindly — see resumability_error's docstring
            yield f"\n[{error}]"
            _record_turn_metrics(time.monotonic() - start, "error", final_state)
            return
        for state in graph.stream(
            Command(resume=False), config=cfg, stream_mode="values"
        ):
            final_state = state
            if time.monotonic() > deadline:
                yield "\n[stopped: request exceeded timeout]"
                _record_turn_metrics(time.monotonic() - start, "timeout", final_state)
                return
            delta = _delta_for(state, printed)
            if delta:
                printed += delta
                yield delta

    _record_turn_metrics(
        time.monotonic() - start, _turn_outcome(final_state), final_state
    )


def _invoke_with_timeout(graph, graph_input, cfg: dict, start: float):
    """graph.invoke() in the executor, bounded by REQUEST_TIMEOUT_SECONDS,
    with the friendly-timeout / error-metric handling answer() needs at
    every invoke call site — the original turn AND the auto-decline resume
    below both need it. Returns None on timeout (a real result is always a
    truthy dict); re-raises other exceptions after recording them."""
    future = _REQUEST_EXECUTOR.submit(graph.invoke, graph_input, config=cfg)
    try:
        return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
    except FuturesTimeoutError:
        _record_turn_metrics(time.monotonic() - start, "timeout")
        return None
    except Exception:
        _record_turn_metrics(time.monotonic() - start, "error")
        raise


@observe(name="chat-turn")
def answer(text: str, thread_id: str, ctx: SecurityCtx) -> tuple[str, list[dict]]:
    """Run one turn and return (final assistant text, used_citations) (used
    by the API). `used_citations` is state["used_citations"] as computed by
    graph.py's check_output — already filtered to the markers the answer
    text actually references, never the full retrieved set (GRAPH_PATTERNS.md
    pattern 20). Empty on every early-return path below (timeout, auto-decline
    error) since there's no real model answer to have cited anything.

    `ctx` is required — see stream_turn's matching docstring note."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    cfg = _config(thread_id, ctx)
    start = time.monotonic()

    result = _invoke_with_timeout(
        graph, {"messages": [HumanMessage(content=text)]}, cfg, start
    )
    if result is None:
        return "Sorry, that took too long to answer — please try again.", []

    if graph.get_state(cfg).next:
        # See stream_turn's matching comment: POST /chat is single-shot
        # with no way to solicit a real approval decision, so auto-decline
        # rather than leave the run paused forever or silently run the
        # tool call.
        metrics.agent_unattended_pause_total.inc()
        error = resumability_error(graph, cfg)
        if error:  # never resume blindly — see resumability_error's docstring
            _record_turn_metrics(time.monotonic() - start, "error")
            return f"Sorry, I couldn't complete that — {error}", []
        result = _invoke_with_timeout(graph, Command(resume=False), cfg, start)
        if result is None:
            return "Sorry, that took too long to answer — please try again.", []

    _record_turn_metrics(time.monotonic() - start, _turn_outcome(result), result)
    return result["messages"][-1].content, result.get("used_citations") or []


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
            from langfuse import Langfuse
            lf = Langfuse()
            trace = lf.trace(name=name, session_id=session_id, input=input_text)
            # stateful_client (not trace_id) is CallbackHandler's actual
            # parameter for this in langfuse==2.60.10 — passing trace_id
            # raises TypeError, which used to be silently swallowed here,
            # leaving every graph node span unreported.
            callbacks.append(
                CallbackHandler(stateful_client=trace, session_id=session_id)
            )
        except Exception:  # noqa: BLE001 - Langfuse optional
            pass
    return trace, callbacks


async def _run_graph_stream(graph, graph_input, cfg, trace):
    """Shared core of astream_events_turn/astream_events_resume: drive one
    graph.astream_events() call, translate events to the app's typed event
    shapes, and yield exactly one terminal event:
      {"type": "approval_required", "tool_calls": [{"name":..., "args":...}]}
        — the run paused at human_approval's interrupt() (see graph.py);
          call astream_events_resume(thread_id, approved) to continue.
      {"type": "citations", "items": [...]} — emitted right before "done",
        only when the answer actually cited something; same
        state["used_citations"] shape as ChatResponse.citations (app/schemas.py).
      {"type": "done"}     — the turn actually finished.
      {"type": "error", "content": "<message>"} — it raised.
    Handles Langfuse trace update/flush and turn metrics identically for
    both entry points.
    """
    start = time.monotonic()
    final_answer = []
    used_citations: list[dict] = []
    try:
        async for event in _iterate_with_timeout(
            graph.astream_events(graph_input, config=cfg, version="v2"),
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
        terminal_event = {"type": "error", "content": str(exc)}
    else:
        # astream_events() simply stops yielding once the run pauses at an
        # interrupt() — there's no exception and no distinct "paused" event,
        # so the only reliable way to tell "paused" from "finished" is to
        # check get_state().next afterwards, same as app/hitl_demo.py's
        # sync loop.
        state = graph.get_state(cfg)
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
            if trace:
                trace.update(output="".join(final_answer))
            _record_turn_metrics(
                time.monotonic() - start, _turn_outcome(state.values), state.values
            )
            used_citations = state.values.get("used_citations") or []
            terminal_event = {"type": "done"}
    finally:
        # Flush so the trace is sent even if the caller exits immediately —
        # runs for all three terminal outcomes above (error, paused, done).
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:  # noqa: BLE001
                pass

    if used_citations:
        yield {"type": "citations", "items": used_citations}
    yield terminal_event


async def astream_events_turn(
    text: str, thread_id: str, ctx: SecurityCtx, require_approval: bool = False
):
    """Production async generator — yields typed event dicts (see
    _run_graph_stream for the full shape list, including
    "approval_required").

    `ctx` is required — see stream_turn's matching docstring note.
    `require_approval` mirrors graph.py's opt-in HITL gate (see
    should_continue): when True, a tool call pauses the run instead of
    executing immediately, and the caller must resume via
    astream_events_resume(thread_id, approved, ctx) to continue — the
    streaming counterpart to app/hitl_demo.py's blocking
    Command(resume=...) loop. Default False keeps existing callers
    (app/api.py) unchanged.
    """
    graph = await init_graph_async()
    _ensure_seeded(graph, thread_id)
    trace, callbacks = _open_trace("chat-turn-stream", thread_id, text)
    cfg = {
        "configurable": {"thread_id": thread_id, "ctx": ctx},
        "callbacks": callbacks,
        "recursion_limit": 12,
    }
    graph_input = {
        "messages": [HumanMessage(content=text)],
        "require_approval": require_approval,
    }
    async for event in _run_graph_stream(graph, graph_input, cfg, trace):
        yield event


async def astream_events_resume(thread_id: str, approved: bool, ctx: SecurityCtx):
    """Resume a turn paused by astream_events_turn(require_approval=True) —
    the streaming counterpart to app/hitl_demo.py's
    `graph.invoke(Command(resume=approved), config)`. `thread_id` must
    match the paused turn.

    `ctx` is required and re-supplied here, not reused from the original
    pause: `config["configurable"]` does NOT persist across a resume
    automatically (verified empirically) — whatever's resolving the
    approval is expected to re-assert who they are, the same way the
    original request had to. This is also where a stricter check (e.g.
    "the resuming principal must match the tenant that paused") would
    naturally go if this app ever needed one; today `resumability_error`
    below only checks the checkpoint's build compatibility, not the
    resumer's identity against the pauser's.

    Opens its own Langfuse trace rather than extending the original one: a
    human's approval can take arbitrarily long (minutes, hours), so this is
    modeled as a separate trace correlated by session_id, not one span kept
    open across the wait — same reasoning as stream_turn's two-trace HITL
    behavior. That same "arbitrarily long" wait is exactly why
    resumability_error is checked here: a human might approve hours (or a
    redeploy) after the pause, so this is the one entry point most likely
    to actually hit a stale or incompatible checkpoint in practice.
    """
    graph = await init_graph_async()
    error = resumability_error(graph, {"configurable": {"thread_id": thread_id}})
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
        "recursion_limit": 12,
    }
    async for event in _run_graph_stream(graph, Command(resume=approved), cfg, trace):
        yield event


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
            # stateful_client, not trace_id — see the matching note in
            # astream_events_turn.
            callbacks.append(
                CallbackHandler(stateful_client=trace, session_id=session_id)
            )
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
    graph = await init_graph_async()
    _ensure_seeded(graph, thread_id)

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
