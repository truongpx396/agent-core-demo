"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the two entry points `stream_turn` (CLI, streaming) and
`answer` (API, single response). Keeping this in one place means memory and
tracing behave identically no matter which front-end is used.

Production streaming is also provided via `astream_events_turn` — see below.

## Durable checkpointing (init_graph_sync / init_graph_async)

The graph is built with an `AsyncPostgresSaver` (survives a process
restart, and — unlike a single SQLite file — is safe under concurrent
access from multiple OS processes at once), not `build_graph()`'s
bare-call default `MemorySaver` (gone the moment the process exits) — a
paused human_approval gate is only a meaningful safety control if the
pause actually survives a redeploy while someone reviews it. Postgres
over SQLite specifically because this app now runs as more than one
process sharing one checkpoint store (the FastAPI process plus one or
more independently-scaled `app/turns/agent_worker.py` processes, all attaching
to the same `thread_id`s) — a single SQLite file's writer-locking is
fragile under that; Postgres is built for it.

Why two init functions instead of one: `AsyncPostgresSaver`'s async
lock/state is bound to whichever asyncio event loop it was created on.
Its SYNC methods (used by `graph.invoke`/`graph.stream`, i.e.
answer()/stream_turn()) work correctly when called from any *other*
thread than that loop — that's the documented, supported cross-thread
path. Its ASYNC methods (used by `graph.ainvoke`/`graph.astream_events`,
i.e. astream_events_turn/_resume) do NOT: calling them from a different
loop than the one they were created on raises "bound to a different event
loop" (asyncio locks are loop-bound, verified empirically before writing
this, originally against AsyncSqliteSaver — the same driver-level
constraint holds for AsyncPostgresSaver). So which loop the checkpointer
is opened on matters:

- `init_graph_async()` — for a process with an asyncio loop it keeps
  alive for its whole lifetime (FastAPI's lifespan, running on uvicorn's
  loop; the CLI's `--stream` mode, running inside `asyncio.run()`). Opens
  the checkpointer directly on the CALLING (current) loop. Async graph
  calls then run natively on the same loop the checkpointer is bound to;
  sync calls (e.g. FastAPI's `/chat` running a plain `def` in its own
  threadpool) still work via the cross-thread path since that's a
  different thread than the loop's.
- `init_graph_sync()` — for a process with NO event loop of its own (the
  CLI's plain, non-`--stream` mode, and scripts/hitl_demo.py). Spins up one
  small background thread hosting a persistent event loop purely to give
  the checkpointer somewhere to live; sync graph calls reach it from any
  other thread exactly as above. This process path never calls the async
  graph methods, so the background loop only ever needs to service
  sync-dispatched calls — the constraint above never bites.

`get_graph()` falls back to `init_graph_sync()` if nothing has initialized
the singleton yet, so it stays a safe drop-in for any purely-sync caller.
An async entry point (astream_events_turn/_resume) MUST have had
`init_graph_async()` awaited first by its own process's startup path (see
app/api/main.py's lifespan, app/channels/chat.py's async_main) — calling it after a sync
path already initialized the singleton would try to reuse a
background-thread-bound checkpointer from a different loop and hit the
same "bound to a different event loop" failure this design exists to avoid.
"""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.agent.graph import (
    CANCEL_SENTINEL,
    build_graph,
    resumability_error,
    resumability_error_async,
)
from app.core import metrics
from app.core.config import (
    CHAT_MODEL,
    CHECKPOINTER_DATABASE_URL,
    MAX_COST_USD_PER_TENANT_PER_DAY,
)
from app.core.errors import ErrorCode, ErrorEnvelope, TurnCancelled
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_checkpointer_cm = None  # keeps AsyncPostgresSaver's context manager alive —
# from_conn_string is an @asynccontextmanager; letting the returned object
# get garbage-collected closes the connection out from under the saver
# (verified empirically against AsyncSqliteSaver's identical shape: the
# very next call fails "no active connection" — the same contract holds
# for AsyncPostgresSaver).
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


def _record_turn_metrics(
    elapsed: float,
    outcome: str,
    state: dict | None = None,
    ctx: SecurityCtx | None = None,
    thread_id: str | None = None,
) -> None:
    metrics.agent_requests_total.labels(outcome=outcome).inc()
    metrics.agent_latency_seconds.observe(elapsed)
    if state is not None:
        metrics.agent_iterations.observe(state.get("iterations", 0))
        total_tokens = state.get("total_tokens", 0)
        if total_tokens:
            metrics.agent_tokens_total.inc(total_tokens)
            # Real usage ledger (GRAPH_PATTERNS.md pattern 26) — only at a
            # call site that actually has ctx/thread_id (a completed turn),
            # never at every early-return timeout/error branch above, which
            # would mean threading both through call sites that already
            # have nothing meaningful to record (total_tokens is 0 there
            # regardless). meter.record_usage degrades to a no-op on its
            # own failure — see its docstring — so this can't fail the turn.
            if ctx is not None and thread_id is not None:
                from app.agent import meter

                meter.record_usage(ctx, thread_id, CHAT_MODEL, total_tokens)


_TENANT_BUDGET_WARNING_FRACTION = 0.8  # log/count once a tenant crosses 80% of its daily cap


def _tenant_over_daily_budget(ctx: SecurityCtx | None) -> bool:
    """True if `ctx`'s tenant has already spent >= MAX_COST_USD_PER_TENANT_PER_DAY
    over the last rolling 24 hours (app/agent/meter.py's usage_ledger) — checked
    BEFORE a turn starts (answer/stream_turn/astream_events_turn), so an
    over-budget tenant is refused without ever reaching the LLM/tool loop
    at all. Distinct from MAX_COST_USD_PER_TURN
    (app/agent/graph.py::should_continue): that ceiling only ever sees ONE
    turn's own running total and has no memory of what the same tenant
    already spent on turns before it — this is the ceiling that
    accumulates ACROSS turns.

    Fails OPEN (never blocks a turn) if the ledger read itself fails — a
    usage-ledger outage must not ALSO take down every turn on top of
    whatever already took the ledger down, same degrade-don't-crash
    posture as meter.record_usage's own write path and
    app/retrieval/semantic_cache.py/app/agent/moderation.py's read paths.
    """
    if not valid_ctx(ctx):
        return False
    from app.agent import meter

    try:
        since = datetime.now(UTC) - timedelta(hours=24)
        spent = meter.usage_summary(ctx["tenant"], since=since)["total_cost_usd"]
    except Exception as exc:  # noqa: BLE001 - a ledger read failing must not also block every turn
        logger.warning(
            "tenant_budget_check_failed", extra={"error_class": type(exc).__name__}
        )
        return False

    if spent >= MAX_COST_USD_PER_TENANT_PER_DAY:
        metrics.agent_tenant_budget_exceeded_total.inc()
        return True
    if spent >= _TENANT_BUDGET_WARNING_FRACTION * MAX_COST_USD_PER_TENANT_PER_DAY:
        metrics.agent_tenant_budget_warning_total.inc()
        logger.warning(
            "tenant_approaching_daily_budget",
            extra={
                "tenant": ctx["tenant"],
                "spent_usd": spent,
                "limit_usd": MAX_COST_USD_PER_TENANT_PER_DAY,
            },
        )
    return False


def _tenant_budget_envelope() -> ErrorEnvelope:
    return ErrorEnvelope(
        code=ErrorCode.TENANT_BUDGET_EXCEEDED,
        message="This tenant's daily usage budget has been reached. Please try again later.",
    )


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


def _upsert_session(ctx: SecurityCtx | None, thread_id: str, text: str) -> None:
    """Record/refresh this thread_id in the session directory (item #9's
    switcher, app/agent/sessions.py) — called at the START of every turn
    (stream_turn/answer/astream_events_turn), unlike
    meter.record_usage/_record_turn_metrics which only fire on a
    completed turn with real token usage. Deliberate: a rejected,
    moderated, or otherwise short-circuited turn still represents a real
    conversation the user started on this thread_id and should still show
    up in "switch conversation," even though it has nothing to meter.
    upsert_session degrades to a no-op on its own failure (see its
    docstring) — this can't fail the turn."""
    from app.agent import sessions

    sessions.upsert_session(ctx, thread_id, text)


def _turn_outcome(state: dict) -> str:
    # validate_input resets iterations to 0 every turn; it only stays 0 if
    # the turn never reached `agent` at all, i.e. the reject_input path.
    return "rejected" if state.get("iterations", 0) == 0 else "success"


async def _iterate_with_timeout(aiter, timeout_seconds: float, cancel_check=None):
    """Wrap an async iterator so the whole run aborts if total wall-clock
    time exceeds `timeout_seconds` — enforces REQUEST_TIMEOUT_SECONDS for
    the astream_events paths. Raises TimeoutError on the next pending
    event; callers already have a generic `except Exception` around this
    loop (for Langfuse error-marking), so no separate handling is needed.

    `cancel_check` (optional, `Callable[[], Awaitable[bool]]`) is polled
    once per loop iteration, before waiting on the next upstream event —
    if it returns True, raises `TurnCancelled` instead of continuing.
    Same caveat as the timeout above: this bounds how long the CALLER
    waits between checkpoints, not how long a single already-in-flight
    upstream step (one slow tool call, one slow model response) keeps
    running underneath — cancellation takes effect at the next event
    boundary, not instantly. Used by app/turns/agent_worker.py to let
    `POST /chat/cancel` (app/api/main.py) stop an actively-streaming (not yet
    paused) turn via a Redis flag (app/turns/queue.py::is_cancelled) — every
    other caller (the in-process `/chat/stream` path, app/channels/chat.py) passes
    nothing here and keeps today's timeout-only behavior unchanged.
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


async def _open_checkpointer():
    """Open the AsyncPostgresSaver on whichever loop calls this — see this
    module's docstring for why the calling loop matters. Returns the saver;
    stashes the context manager in the module global so it isn't
    garbage-collected out from under the connection. `saver.setup()` is
    idempotent (creates its checkpoints/checkpoint_blobs/checkpoint_writes
    tables on first run only) — safe to call on every process start,
    including every independently-scaled agent_worker.py instance."""
    global _checkpointer_cm
    cm = AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL)
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
    own (the CLI's plain mode; scripts/hitl_demo.py) — see this module's
    docstring. Starts one small daemon thread hosting a persistent loop
    purely so AsyncPostgresSaver has somewhere to live; that thread is
    never explicitly stopped (a daemon thread dies with the process, and
    Postgres's own WAL makes an abrupt disconnect safe — same as a real
    deployment being SIGKILLed)."""
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
        except Exception:  # noqa: BLE001, S110 - Langfuse optional, no session_id means no callback
            pass
    return callbacks


def _ensure_seeded(graph, thread_id: str) -> None:
    """Seed a new conversation thread with the system prompt exactly once
    — the SYNC path, for stream_turn/answer only (both reach the graph
    via get_graph(), which for the durable singleton is init_graph_sync()'s
    background-thread-hosted checkpointer; this sync `update_state` call
    runs on the CALLING thread, a different one than the checkpointer's
    own — the documented, supported cross-thread path, see this module's
    docstring). `astream_events_turn`/`astream_events_turn_ctx` run
    directly ON the checkpointer's own loop instead and MUST use
    `_ensure_seeded_async` below — calling this sync version from there
    raises `asyncio.InvalidStateError` (the checkpointer refuses a sync
    call from the same loop it was created on — originally caught against
    `AsyncSqliteSaver`; the same loop-binding constraint holds for
    `AsyncPostgresSaver`), caught empirically via a real `POST /chat/stream`
    request against a fresh thread_id before this split existed.

    Reads `graph.manifest.system_prompt` (stamped by build_graph — see
    GRAPH_PATTERNS.md pattern 23, app/agent/manifest.py) rather than the
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


async def _ensure_seeded_async(graph, thread_id: str) -> None:
    """The ASYNC counterpart to `_ensure_seeded` — see its docstring for
    why the split exists. Used by `astream_events_turn`/
    `astream_events_turn_ctx`, both of which run directly on the
    checkpointer's own event loop (via `init_graph_async()`), where only
    the checkpointer's async methods (`aupdate_state`, not
    `update_state`) are safe to call."""
    if thread_id in _seeded:
        return
    await graph.aupdate_state(
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


def _build_human_content(text: str, images: list[str] | None) -> str | list[str | dict]:
    """Plain `str` when there's no image — the overwhelmingly common case,
    and what every existing caller/test already expects. A multimodal
    content list (GRAPH_PATTERNS.md pattern 44) — OpenAI/LiteLLM's
    documented `[{"type": "text", ...}, {"type": "image_url", ...}]`
    shape — only when at least one image is actually attached, so a
    text-only turn produces byte-identical `HumanMessage` content to
    before this feature existed.

    `images` are data URIs or plain URLs; this app never fetches or
    decodes them itself — whatever model is configured behind
    `CHAT_MODEL` does that, the same way it already resolves text tokens.
    A model with no vision capability just ignores or errors on the
    image part, exactly as it would for any other request it can't
    fulfill; this app doesn't detect vision capability up front (see
    GRAPH_PATTERNS.md pattern 44's scope notes).
    """
    if not images:
        return text
    parts: list[str | dict] = [{"type": "text", "text": text}]
    parts.extend({"type": "image_url", "image_url": {"url": img}} for img in images)
    return parts


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
def stream_turn(text: str, thread_id: str, ctx: SecurityCtx, images: list[str] | None = None):
    """Yield the assistant's answer incrementally (used by the CLI).

    `ctx` is required, not optional-with-a-default: a caller with no valid
    tenant/principal to stamp shouldn't silently get a default identity
    (see app/core/security.py's SecurityCtx docstring) — graph.py's
    validate_input/route_after_validation fail closed on it regardless,
    but making it a required parameter here means that failure is visible
    at the call site, not just three nodes deep in the graph.

    `images` (GRAPH_PATTERNS.md pattern 44) is optional and defaults to
    None — every existing caller keeps working unchanged.
    """
    langfuse_context.update_current_trace(session_id=thread_id)
    start = time.monotonic()
    if _tenant_over_daily_budget(ctx):
        yield "\n[This tenant's daily usage budget has been reached. Please try again later.]"
        _record_turn_metrics(time.monotonic() - start, "rejected")
        return
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    _upsert_session(ctx, thread_id, text)
    deadline = start + REQUEST_TIMEOUT_SECONDS
    printed = ""
    final_state: dict = {}
    cfg = _config(thread_id, ctx)

    for state in graph.stream(
        {"messages": [HumanMessage(content=_build_human_content(text, images))]},
        config=cfg,
        stream_mode="values",
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
        time.monotonic() - start, _turn_outcome(final_state), final_state,
        ctx=ctx, thread_id=thread_id,
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


_TIMEOUT_MESSAGE = "Sorry, that took too long to answer — please try again."


def _timeout_envelope() -> ErrorEnvelope:
    return ErrorEnvelope(code=ErrorCode.TIMEOUT, message=_TIMEOUT_MESSAGE)


def _resumability_envelope(error: str) -> ErrorEnvelope:
    """`resumability_error`'s return value is a human-readable string that
    always starts with its own error code as a prefix (`"checkpoint_lost:
    ..."` or `"checkpoint_incompatible: ..."` — see its docstring) — this
    just maps that prefix onto the canonical `ErrorCode` registry rather
    than re-deriving the classification from scratch."""
    code = (
        ErrorCode.CHECKPOINT_LOST
        if error.startswith("checkpoint_lost")
        else ErrorCode.CHECKPOINT_INCOMPATIBLE
    )
    return ErrorEnvelope(
        code=code, message=f"Sorry, I couldn't complete that — {error}"
    )


@observe(name="chat-turn")
def answer(
    text: str, thread_id: str, ctx: SecurityCtx, images: list[str] | None = None
) -> tuple[str, list[dict], ErrorEnvelope | None, int]:
    """Run one turn and return (final assistant text, used_citations,
    error, ungrounded_claims_count) (used by the API). `used_citations` is
    state["used_citations"] as computed by graph.py's check_output —
    already filtered to the markers the answer text actually references,
    never the full retrieved set (GRAPH_PATTERNS.md pattern 20). `error`
    is the canonical `ErrorEnvelope` (GRAPH_PATTERNS.md pattern 30,
    app/core/errors.py) — `None` for a normal success OR a successful
    auto-decline (both produce a real answer text, just not one from a
    genuine failure); set when the turn ended in an actual TIMEOUT, a
    stale/incompatible checkpoint it refused to resume into, or this
    tenant's rolling 24h spend already reached
    MAX_COST_USD_PER_TENANT_PER_DAY (`_tenant_over_daily_budget`) — that
    last case never even reaches the graph.
    `ungrounded_claims_count` (pattern 39) is `state["ungrounded_claims_count"]`
    — how many cited markers in the answer didn't match any real
    citation. `used_citations` and `ungrounded_claims_count` are both `0`/
    empty on every error path — there's no real model answer to have
    cited anything.

    `ctx` is required — see stream_turn's matching docstring note.
    `images` (GRAPH_PATTERNS.md pattern 44) is optional, defaulting to
    None — every existing caller keeps working unchanged."""
    langfuse_context.update_current_trace(session_id=thread_id)
    start = time.monotonic()
    if _tenant_over_daily_budget(ctx):
        _record_turn_metrics(time.monotonic() - start, "rejected")
        envelope = _tenant_budget_envelope()
        return envelope.message, [], envelope, 0
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    _upsert_session(ctx, thread_id, text)
    cfg = _config(thread_id, ctx)

    result = _invoke_with_timeout(
        graph, {"messages": [HumanMessage(content=_build_human_content(text, images))]}, cfg, start
    )
    if result is None:
        return _TIMEOUT_MESSAGE, [], _timeout_envelope(), 0

    if graph.get_state(cfg).next:
        # See stream_turn's matching comment: POST /chat is single-shot
        # with no way to solicit a real approval decision, so auto-decline
        # rather than leave the run paused forever or silently run the
        # tool call.
        metrics.agent_unattended_pause_total.inc()
        error = resumability_error(graph, cfg)
        if error:  # never resume blindly — see resumability_error's docstring
            _record_turn_metrics(time.monotonic() - start, "error")
            envelope = _resumability_envelope(error)
            return envelope.message, [], envelope, 0
        result = _invoke_with_timeout(graph, Command(resume=False), cfg, start)
        if result is None:
            return _TIMEOUT_MESSAGE, [], _timeout_envelope(), 0

    _record_turn_metrics(
        time.monotonic() - start, _turn_outcome(result), result, ctx=ctx, thread_id=thread_id
    )
    return (
        result["messages"][-1].content,
        result.get("used_citations") or [],
        None,
        result.get("ungrounded_claims_count") or 0,
    )


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
        except Exception:  # noqa: BLE001, S110 - Langfuse optional
            pass
    return trace, callbacks


async def _run_graph_stream(graph, graph_input, cfg, trace, cancel_check=None):
    """Shared core of astream_events_turn/astream_events_resume: drive one
    graph.astream_events() call, translate events to the app's typed event
    shapes, and yield exactly one terminal event:
      {"type": "approval_required", "tool_calls": [{"name":..., "args":...}]}
        — the run paused at human_approval's interrupt() (see graph.py);
          call astream_events_resume(thread_id, approved) to continue.
      {"type": "citations", "items": [...]} — emitted right before "done",
        only when the answer actually cited something; same
        state["used_citations"] shape as ChatResponse.citations (app/api/schemas.py).
      {"type": "followups", "items": ["...", ...]} — emitted right before
        "done", only when suggest_followups (pattern 27) produced any;
        same state["followups"] shape, never sent for a cache hit or an
        uncited answer (see suggest_followups's own docstring).
      {"type": "done"}     — the turn actually finished.
      {"type": "error", "content": "<message>"} — it raised, INCLUDING a
        user-initiated stop (`code: "cancelled"`, see TurnCancelled below)
        — modeled as a terminal-outcome envelope like any other, not a
        separate SSE event type (GRAPH_PATTERNS.md pattern 30).
    Handles Langfuse trace update/flush and turn metrics identically for
    both entry points. `cancel_check` (optional) is forwarded straight to
    `_iterate_with_timeout` — see its docstring; only app/turns/agent_worker.py's
    `"turn"`-job dispatch passes one today.
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

            if kind == "on_chat_model_stream":
                # Raw token chunk — content may be str or list (multimodal)
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

    except TurnCancelled:
        # Checked BEFORE the generic except below — a deliberate stop is
        # not "an unexpected failure," it gets its own ErrorCode rather
        # than falling into ErrorCode.INTERNAL alongside a real bug.
        if trace:
            trace.update(
                output="".join(final_answer) + " [cancelled by user]", level="WARNING"
            )
        _record_turn_metrics(time.monotonic() - start, "cancelled")
        metrics.agent_streaming_cancellation_total.inc()
        envelope = ErrorEnvelope(code=ErrorCode.CANCELLED, message="Cancelled by user.")
        terminal_event = {"type": "error", "content": envelope.message, **envelope.to_dict()}
    except Exception as exc:  # noqa: BLE001
        if trace:
            trace.update(output=f"error: {exc}", level="ERROR")
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        _record_turn_metrics(time.monotonic() - start, outcome)
        # asyncio.wait_for's OWN internal timeout (inside
        # _iterate_with_timeout, when a single step — not the overall
        # deadline check — runs out of remaining budget) raises a bare
        # TimeoutError with an EMPTY str(exc) — verified against a real
        # slow local-model turn before adding this fallback message, since
        # a blank {"type": "error", "content": ""} SSE event tells a
        # client/UI nothing useful about what happened.
        message = (
            f"Request exceeded {REQUEST_TIMEOUT_SECONDS}s timeout"
            if outcome == "timeout"
            else str(exc)
        )
        envelope = ErrorEnvelope(
            code=ErrorCode.TIMEOUT if outcome == "timeout" else ErrorCode.INTERNAL,
            message=message,
        )
        # "content" kept alongside the envelope fields for backward
        # compatibility with existing consumers (app/channels/chat.py, the web UI)
        # that already read event["content"] — see app/core/errors.py's module
        # docstring for why the envelope is additive here, not a rename.
        terminal_event = {"type": "error", "content": message, **envelope.to_dict()}
    else:
        # astream_events() simply stops yielding once the run pauses at an
        # interrupt() — there's no exception and no distinct "paused" event,
        # so the only reliable way to tell "paused" from "finished" is to
        # check get_state().next afterwards, same as scripts/hitl_demo.py's
        # sync loop. aget_state, not get_state: this async function runs
        # directly on the checkpointer's own event loop (via
        # init_graph_async()), where only the async accessor is safe to
        # call — see resumability_error_async's docstring for the same
        # constraint, caught here by a real regression test.
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
            if trace:
                trace.update(output="".join(final_answer))
            _record_turn_metrics(
                time.monotonic() - start,
                _turn_outcome(state.values),
                state.values,
                ctx=(cfg.get("configurable") or {}).get("ctx"),
                thread_id=(cfg.get("configurable") or {}).get("thread_id"),
            )
            used_citations = state.values.get("used_citations") or []
            ungrounded_claims_count = state.values.get("ungrounded_claims_count") or 0
            followups = state.values.get("followups") or []
            if not final_answer:
                # No on_chat_model_stream events fired this turn — every
                # node that produces a final AIMessage WITHOUT calling the
                # chat model (reject_input, reject_context,
                # reject_moderation, context_window_exceeded, and a
                # semantic-cache HIT, pattern 22) hits this: `final_answer`
                # only ever accumulates from token-streaming events, so a
                # turn that never streamed anything left the caller with
                # nothing but a bare "done" and no way to learn what the
                # answer actually was — a real, previously-undiscovered
                # bug, caught live against a cached "hi" response that
                # streamed only {"type": "done"} despite a real cached
                # answer sitting in state.values["messages"][-1]. Sent as
                # one synthetic "token" event (not a new event type) so
                # every existing client already renders it correctly.
                final_message = state.values["messages"][-1]
                skipped_text = (
                    final_message.content if isinstance(final_message.content, str) else ""
                )
                if skipped_text:
                    yield {"type": "token", "content": skipped_text}
            terminal_event = {"type": "done"}
    finally:
        # Flush so the trace is sent even if the caller exits immediately —
        # runs for all three terminal outcomes above (error, paused, done).
        if trace:
            try:
                from langfuse import Langfuse
                Langfuse().flush()
            except Exception:  # noqa: BLE001, S110 - best-effort flush on the way out
                pass

    if used_citations or ungrounded_claims_count:
        yield {
            "type": "citations",
            "items": used_citations,
            "ungrounded_claims_count": ungrounded_claims_count,
        }
    if followups:
        # suggest_followups (GRAPH_PATTERNS.md pattern 27) computes real
        # follow-up questions into state["followups"], but this streaming
        # path never surfaced them — a real, previously-undiscovered gap:
        # the web UI already ships CSS for rendering them as clickable
        # suggestion chips (app/api/static/index.html's .followups/.followups
        # button classes) but had no event to populate it from, and no
        # client code ever reads this field, so it was silently dead.
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
    _run_graph_stream for the full shape list, including
    "approval_required").

    `ctx` is required — see stream_turn's matching docstring note.
    `require_approval` mirrors graph.py's opt-in HITL gate (see
    should_continue): when True, a tool call pauses the run instead of
    executing immediately, and the caller must resume via
    astream_events_resume(thread_id, approved, ctx) to continue — the
    streaming counterpart to scripts/hitl_demo.py's blocking
    Command(resume=...) loop. Default False keeps existing callers
    (app/api/main.py) unchanged. `images` (GRAPH_PATTERNS.md pattern 44) is
    optional, defaulting to None — same reasoning. `cancel_check`
    (optional, `Callable[[], Awaitable[bool]]`) is forwarded straight to
    `_run_graph_stream`/`_iterate_with_timeout` — see their docstrings;
    only app/turns/agent_worker.py's `"turn"`-job dispatch passes one, wiring it
    to a Redis flag `POST /chat/cancel` sets (app/turns/queue.py::is_cancelled).

    Refused up front (an `error` event, never reaching the graph) if this
    tenant's rolling 24h spend already reached
    MAX_COST_USD_PER_TENANT_PER_DAY — see `_tenant_over_daily_budget`.
    """
    if _tenant_over_daily_budget(ctx):
        metrics.agent_requests_total.labels(outcome="rejected").inc()
        envelope = _tenant_budget_envelope()
        yield {"type": "error", "content": envelope.message, **envelope.to_dict()}
        return
    graph = await init_graph_async()
    await _ensure_seeded_async(graph, thread_id)
    _upsert_session(ctx, thread_id, text)
    trace, callbacks = _open_trace("chat-turn-stream", thread_id, text)
    cfg = {
        "configurable": {"thread_id": thread_id, "ctx": ctx},
        "callbacks": callbacks,
        "recursion_limit": 12,
    }
    graph_input = {
        "messages": [HumanMessage(content=_build_human_content(text, images))],
        "require_approval": require_approval,
    }
    async for event in _run_graph_stream(graph, graph_input, cfg, trace, cancel_check=cancel_check):
        yield event


async def astream_events_turn_unattended(
    text: str,
    thread_id: str,
    ctx: SecurityCtx,
    require_approval: bool = False,
    images: list[str] | None = None,
):
    """astream_events_turn, but auto-declines a single approval_required
    pause instead of yielding it and waiting for astream_events_resume —
    for a caller with no interactive human on the other end of THIS call.
    Today that's app/turns/agent_worker.py (the Redis Streams queue's consumer
    side, GRAPH_PATTERNS.md pattern 43): a request pulled off a shared
    queue has no round trip back to whoever originally asked, so it must
    resolve any pause on its own rather than leave the run paused forever.

    Same one-round auto-decline shape answer()/stream_turn() already use
    for their own single-shot callers (pattern 8) — not a loop: a SECOND
    pause on the same turn (e.g. a rejected action followed by another
    mutating attempt) is left to the model's own next response, exactly
    like those two functions already handle it.
    """
    paused = False
    async for event in astream_events_turn(
        text, thread_id, ctx, require_approval=require_approval, images=images
    ):
        if event.get("type") == "approval_required":
            paused = True
            continue
        yield event
    if paused:
        metrics.agent_unattended_pause_total.inc()
        async for event in astream_events_resume(thread_id, False, ctx):
            yield event


async def astream_events_resume(thread_id: str, approved: bool, ctx: SecurityCtx):
    """Resume a turn paused by astream_events_turn(require_approval=True) —
    the streaming counterpart to scripts/hitl_demo.py's
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
        "recursion_limit": 12,
    }
    async for event in _run_graph_stream(graph, Command(resume=approved), cfg, trace):
        yield event


async def cancel_run(thread_id: str, ctx: SecurityCtx) -> bool:
    """Cancel a run paused at human_approval (GRAPH_PATTERNS.md pattern
    36) — resumes it with `graph.CANCEL_SENTINEL` instead of `True`/
    `False`, which `human_approval` treats as a THIRD outcome: the gated
    action never runs, and — unlike a rejection — the model never gets a
    turn to react to it. This is a caller-initiated abort ("stop this
    run"), not feedback for another attempt.

    Returns `True` if a paused run was actually cancelled, `False` if
    there was nothing to cancel (see `resumability_error_async` — the
    same checkpoint_lost/checkpoint_incompatible check every other resume
    path already uses; a stale or missing checkpoint just means there's
    nothing left to abort, not an error worth raising).
    """
    graph = await init_graph_async()
    error = await resumability_error_async(graph, {"configurable": {"thread_id": thread_id}})
    if error:
        return False
    cfg = {"configurable": {"thread_id": thread_id, "ctx": ctx}, "recursion_limit": 12}
    await graph.ainvoke(Command(resume=CANCEL_SENTINEL), config=cfg)
    metrics.agent_cancellation_total.inc()
    return True


async def get_session_messages(thread_id: str) -> list[dict]:
    """The session switcher's transcript replay (item #9, app/api/main.py's
    `GET /chat/sessions/{thread_id}/messages`) — reads the SAME shared
    Postgres checkpointer every other resume/cancel path uses
    (app/agent/sessions.py's own `chat_sessions` table only has a title/
    timestamps, not message content). Ownership (does this thread_id
    belong to the caller's tenant+principal) is the CALLER's
    responsibility to check first via app/agent/sessions.py::list_sessions —
    this function has no ctx to check it against, on purpose: a
    thread_id's checkpoint carries no owner of its own to compare against
    (see app/agent/sessions.py's module docstring on why the checkpointer's own
    tables aren't tenant/principal-scoped at all).

    Returns `[{"role": "user"|"assistant", "text": str}, ...]` — the same
    two roles the web UI already renders bubbles for. Deliberately
    narrower than the full persisted state: `SystemMessage`s (the
    ephemeral history-summary/context injections are never actually
    persisted back to state, but a seeded base prompt is) and
    `ToolMessage`s (tool call/result pairs) are both omitted — this is a
    replay of the conversational back-and-forth a user saw, not a full
    forensic trace (that's what Langfuse is for); an AIMessage with no
    text of its own (a pure tool-calling turn — the model's visible
    answer came on a LATER message once the tool result came back) is
    skipped the same way the live UI never rendered an empty bubble for it.
    """
    graph = await init_graph_async()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    messages = []
    for m in state.values.get("messages", []):
        if isinstance(m, HumanMessage):
            role = "user"
        elif isinstance(m, AIMessage):
            role = "assistant"
        else:
            continue
        text = _text_content(m.content)
        if text:
            messages.append({"role": role, "text": text})
    return messages


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
    graph = await init_graph_async()
    await _ensure_seeded_async(graph, thread_id)

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
            _record_turn_metrics(time.monotonic() - start, outcome)
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
        _record_turn_metrics(
            time.monotonic() - start, _turn_outcome(final_state), final_state
        )

    yield {"type": "done"}
