"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the streaming entry points every front-end actually
calls — `astream_events_turn`/`astream_events_resume` for a caller with a real
interactive human on the other end (the CLI, `app/turns/agent_worker.py`'s
`"turn"`/`"resume"` jobs backing the queued HTTP API) and
`astream_events_turn_unattended` for one with nobody able to answer an
approval prompt (`app/channels/telegram.py`). Keeping this in one place means
memory and tracing behave identically no matter which front-end is used.

## Durable checkpointing (init_graph_async)

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

`AsyncPostgresSaver`'s async lock/state is bound to whichever asyncio
event loop it was *created* on — its async methods (`graph.ainvoke`/
`graph.astream_events`, i.e. astream_events_turn/_resume, the only way
this app ever drives the graph now) raise "bound to a different event
loop" if awaited from a different loop than the one that created it
(asyncio locks are loop-bound, verified empirically before writing this,
originally against AsyncSqliteSaver — the same driver-level constraint
holds for AsyncPostgresSaver). `init_graph_async()` opens the
checkpointer directly on the CALLING (current) loop, so every process
that drives the graph (FastAPI's lifespan, on uvicorn's own loop; the
CLI, inside `asyncio.run()`; app/turns/agent_worker.py;
app/channels/telegram.py) must await it on that SAME loop before making
any graph call — never from a background thread or a different loop.

A sync-checkpointer-access path (`init_graph_sync()`/`get_graph()`, a
background thread hosting a persistent loop purely so `graph.invoke()`
could be called synchronously) existed here for one caller —
`scripts/hitl_demo.py`, a standalone demo of LangGraph's HITL
`interrupt()` pattern — and was removed once that script was, since
`make chat-hitl` (app/channels/chat.py, fully async) already demonstrates
the identical approve/reject pause/resume cycle through this app's real,
production streaming path. Nothing else ever called the sync graph
methods.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import (
    COMPACTION_MARKER_KEY,
    MAX_ITERATIONS,
)
from app.agent.graph_build import build_graph
from app.agent.graph_hitl import CANCEL_SENTINEL, resumability_error_async
from app.core import metrics
from app.core.config import (
    CHAT_MODEL,
    CHECKPOINTER_DATABASE_URL,
    MAX_COST_USD_PER_TENANT_PER_DAY,
    REQUEST_TIMEOUT_SECONDS,
)
from app.core.errors import ErrorCode, ErrorEnvelope, TurnCancelled
from app.core.security import SecurityCtx, valid_ctx

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin

logger = logging.getLogger(__name__)

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_domain_name = "ecorp"  # this process's own domain (app/domains/registry.py),
# set alongside _graph below — used only to stamp app/agent/sessions.py's
# chat_sessions.domain column (GRAPH_PATTERNS.md pattern 49), since the
# graph itself doesn't otherwise need to know its own domain's NAME (only
# its manifest/tools, already baked into `_graph` by build_graph()).
_checkpointer_pool = None  # keeps AsyncPostgresSaver's AsyncConnectionPool
# alive for the process's lifetime — letting it get garbage-collected would
# close every pooled connection out from under the saver (verified
# empirically against the old single-AsyncConnection shape: the very next
# call failed "no active connection" — the same contract holds for a pool).
# Also lets close_checkpointer_pool() below shut it down explicitly on
# graceful shutdown instead of leaving it dangling at process exit.
_seeded: set[str] = set()

# LangGraph's OWN graph-step cap — a coarser, different unit than
# MAX_ITERATIONS (an agent-node-invocation count): every turn also runs
# several fixed pre-loop nodes (validate_input, compact_history,
# moderate_input, check_semantic_cache, retrieve_context) plus a couple more
# post-loop (check_output, write_semantic_cache), and each agent<->tools
# round trip is 2 steps on top of that. A flat "12" here (this constant's
# value before this derivation existed) undercounts for a real model making
# several genuine tool-call round trips in one turn — verified empirically
# against a live Ollama-backed run hitting GraphRecursionError on an
# ordinary multi-tool-call question well before MAX_ITERATIONS (10) was
# reached — the same class of bug independently caught and fixed for the
# nested subagent graph, see GRAPH_PATTERNS.md pattern 46. Derived from
# MAX_ITERATIONS, with real margin, instead of a bare literal, so all four
# call sites below share one source of truth that can't drift out of sync
# with each other or with MAX_ITERATIONS if that ever changes.
RECURSION_LIMIT = MAX_ITERATIONS * 2 + 15


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
    BEFORE a turn starts (astream_events_turn), so an
    over-budget tenant is refused without ever reaching the LLM/tool loop
    at all. Distinct from MAX_COST_USD_PER_TURN
    (app/agent/graph_routing.py::should_continue): that ceiling only ever sees ONE
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
    (astream_events_turn, right after seeding), unlike
    meter.record_usage/_record_turn_metrics which only fire on a
    completed turn with real token usage. Deliberate: a rejected,
    moderated, or otherwise short-circuited turn still represents a real
    conversation the user started on this thread_id and should still show
    up in "switch conversation," even though it has nothing to meter.
    upsert_session degrades to a no-op on its own failure (see its
    docstring) — this can't fail the turn.

    `_domain_name` is THIS PROCESS's own domain (set once, in
    init_graph_async, alongside `_graph` itself) — correct
    regardless of which caller reached here through astream_events_turn: the
    CLI (app/channels/chat.py) and app/channels/telegram.py both stay bound to
    whichever domain their own process booted against, while a queued
    `"turn"` job runs inside whichever app/turns/agent_worker.py POOL picked
    it up, already bound to one AGENT_DOMAIN for its whole life — so the
    session row this stamps always matches the domain that actually ran the
    turn."""
    from app.agent import sessions

    sessions.upsert_session(ctx, thread_id, text, domain=_domain_name)


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
    other caller (app/channels/chat.py, app/channels/telegram.py) passes
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
    stashes the pool in the module global so it isn't garbage-collected out
    from under the connection (see `_checkpointer_pool`'s own comment) and
    so `close_checkpointer_pool()` can shut it down explicitly later.

    Backed by an `AsyncConnectionPool`, not a single `AsyncConnection` (what
    the old `AsyncPostgresSaver.from_conn_string` call opened and held for
    the whole process lifetime) — `AsyncPostgresSaver` accepts either
    (`langgraph.checkpoint.postgres._ainternal.get_connection` branches on
    `isinstance(conn, AsyncConnectionPool)`), and a pool is what actually
    lets concurrent turns overlap instead of every checkpoint read/write
    funneling through one shared connection (psycopg wraps every operation
    on a connection in its own internal lock — verified directly in
    psycopg/connection_async.py — so a single connection serializes
    concurrent callers regardless of how the caller above is written).

    This still isn't full parallelism at the checkpoint layer:
    `AsyncPostgresSaver` wraps its own cursor access in one `asyncio.Lock`
    per saver instance regardless of `conn`'s type
    (`langgraph/checkpoint/postgres/aio.py`'s `_cursor`), so two concurrent
    turns' checkpoint reads/writes still serialize against each other. But
    that lock is only held for one row read/write at a time, not for a
    turn's whole duration — so the actually slow part of a turn (the LLM
    call, tool execution) still runs fully concurrently; only the brief
    "save/load this checkpoint row" moments queue, same as any single write
    path into one Postgres row would. `min_size`/`max_size` mirror
    app/agent/sql_store.py's own pool sizing for the appdata database.

    `saver.setup()` is idempotent (creates its checkpoints/checkpoint_blobs/
    checkpoint_writes tables on first run only) — safe to call on every
    process start, including every independently-scaled agent_worker.py
    instance."""
    global _checkpointer_pool
    pool = AsyncConnectionPool(
        CHECKPOINTER_DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True)
    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()
    _checkpointer_pool = pool
    return saver


def _resolve_domain_name(manifest: "AgentManifest | None") -> str:
    """`manifest.name` if given, else whatever build_graph() itself would
    fall back to (DEFAULT_MANIFEST, "ecorp") — mirrors build_graph()'s own
    `manifest = manifest or DEFAULT_MANIFEST` substitution (app/agent/graph.py)
    exactly, so `_domain_name` always names whichever manifest the graph
    was ACTUALLY built with, never guessed independently of it. Lazy
    import, same reason build_graph() itself imports DEFAULT_MANIFEST
    lazily rather than at module level (see that function's own docstring)."""
    if manifest is not None:
        return manifest.name
    from app.agent.manifest import DEFAULT_MANIFEST

    return DEFAULT_MANIFEST.name


async def close_checkpointer_pool() -> None:
    """Shuts the checkpointer's connection pool down cleanly — same
    reasoning as app/agent/sql_store.py::close_pool for the appdata pool
    (skipping this leaves the pool's background worker tasks/connections
    still open at process exit). Called from graceful-shutdown paths only
    (app/api/main.py's lifespan, app/turns/agent_worker.py::run); a no-op if
    the checkpointer was never opened."""
    global _checkpointer_pool
    if _checkpointer_pool is not None:
        await _checkpointer_pool.close()
        _checkpointer_pool = None


async def init_graph_async(manifest: "AgentManifest | None" = None, domain: "DomainPlugin | None" = None):
    """Initialize (or reuse) the shared graph for a process with its own
    persistent event loop (FastAPI's lifespan; the CLI's --stream mode) —
    see this module's docstring. Returns the graph. A no-op if the
    singleton already exists — both astream_events_turn/_resume call this
    directly precisely so the checkpointer ends up bound to whichever loop
    is *actually* driving them, self-healing even if a process's startup
    path forgot to prime it via lifespan; when startup DID prime it
    already, this is just a cheap existence check (`manifest`/`domain` are
    then ignored — the singleton, once built, doesn't change domain
    mid-process).

    `manifest`/`domain` (GRAPH_PATTERNS.md pattern 23, app/agent/manifest.py)
    are threaded straight into `build_graph()`, which already accepts
    them — both default to `None`, meaning "build_graph()'s own default,
    the Ecorp domain," so every EXISTING caller (app/api/main.py's lifespan,
    app/channels/chat.py's --stream mode) is completely unaffected. This is
    what lets a NEW process boot the exact same durable-checkpointer
    machinery against a DIFFERENT domain instead — see
    app/channels/telegram.py, which reads AGENT_DOMAIN and resolves it via
    app/domains/registry.py before priming the singleton. Each such
    process still serves exactly one domain for its whole lifetime — this
    is not the "several domains from one running process" registry the
    Roadmap describes as still unbuilt (GRAPH_PATTERNS.md's "Extending
    Further"), just "which one domain" becoming a boot-time parameter
    instead of a hardcoded default.
    """
    global _graph, _domain_name
    if _graph is None:
        _domain_name = _resolve_domain_name(manifest)
        saver = await _open_checkpointer()
        _graph = build_graph(checkpointer=saver, manifest=manifest, domain=domain)
    return _graph


async def _ensure_seeded_async(graph, thread_id: str) -> None:
    """Seed a new conversation thread with the system prompt exactly once.
    ASYNC only — `astream_events_turn`/`astream_events_turn_ctx` run
    directly ON the checkpointer's own event loop (via `init_graph_async()`),
    where only the checkpointer's async methods (`aupdate_state`, not
    `update_state`) are safe to call; the sync method raises
    `asyncio.InvalidStateError` from that same loop (the checkpointer
    refuses a sync call from the loop it was created on — originally
    caught against `AsyncSqliteSaver`; the same loop-binding constraint
    holds for `AsyncPostgresSaver` — see this module's docstring), caught
    empirically via a real live-`uvicorn` request against a fresh
    thread_id before this async-only version existed (see
    tests/agent/test_durable_checkpoint.py::TestAsyncSeeding).

    Reads `graph.manifest.system_prompt` (stamped by build_graph — see
    GRAPH_PATTERNS.md pattern 23, app/agent/manifest.py) rather than the
    module-level `SYSTEM_PROMPT` constant, so this seeds the CORRECT
    prompt for whichever domain `graph` was actually built for — every
    graph build_graph() returns always carries a `.manifest`, defaulting
    to the Ecorp domain, so `graph.manifest.system_prompt` and the
    top-level `SYSTEM_PROMPT` import are identical for every caller in
    this app today (init_graph_async never passes a non-default
    manifest); this only starts to matter the day some caller does.

    `_seeded` is only a same-process FAST PATH, never the source of
    truth — real bug, found live via Langfuse: a thread's `agent`
    generation showed the ~800-token system prompt TWICE. `_seeded` is a
    plain in-process `set()`, so it forgets everything on a worker
    restart, and — since `app/turns/agent_worker.py`'s own docstring
    says to run SEVERAL `agent-worker` processes for scaling, with Redis
    Streams distributing turns across them round-robin — a thread's
    later turns can just as easily land on a DIFFERENT process that
    never saw this thread_id before. Either way, the next process thinks
    a long-running thread is brand new and appends a second copy of the
    prompt via `aupdate_state`. That duplicate is permanent, not a
    one-time cost: `_messages_to_trim` (graph.py) excludes every
    SystemMessage from both its token budget AND its removal candidates,
    so nothing ever cleans it up — every later turn on that thread pays
    the extra ~800 tokens forever, directly eating into
    MAX_TOKENS_PER_TURN. Fixed by checking the thread's actual persisted
    state (the real source of truth) before seeding, and only trusting
    `_seeded` to skip that check on a thread this SAME process already
    confirmed — costs one extra `aget_state` per thread per process, not
    per turn.
    """
    if thread_id in _seeded:
        return
    cfg = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(cfg)
    already_seeded = any(
        isinstance(m, SystemMessage) and m.content == graph.manifest.system_prompt
        for m in state.values.get("messages", [])
    )
    if not already_seeded:
        await graph.aupdate_state(
            cfg, {"messages": [SystemMessage(content=graph.manifest.system_prompt)]}
        )
    _seeded.add(thread_id)


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
        — the run paused at human_approval's interrupt() (see graph_hitl.py);
          call astream_events_resume(thread_id, approved) to continue.
      {"type": "retry"} — NOT terminal: graph.py's retry_output just
        rejected the last answer and looped back to `agent` for a fresh
        one. Every "token" event already sent this turn belongs to the
        now-discarded answer — a client must clear whatever it's rendered
        so far before the next "token" arrives, or the rejected and
        retried answers render concatenated with no indication a retry
        ever happened (a real bug, caught live via Langfuse: an uncited
        answer got retried, and the client showed both answers run
        together as one). Fired from `on_chain_end` of the `retry_output`
        node itself, which also resets `final_answer` here so Langfuse's
        own `output` field doesn't show the same concatenation. ALSO fired
        from `on_chain_end` of `retry_exhausted` (route_after_check giving
        up on a stuck retry loop, see MAX_CONSECUTIVE_SAME_RETRY_REASON) —
        but ONLY when that node actually replaced the last message rather
        than trusting it (graph.py's _TRUST_CONTENT_RETRY_REASONS — some
        rejection reasons, like a real answer just missing its citation
        marker, are attribution nitpicks the content stays trustworthy
        despite, and retry_exhausted no-ops for those, leaving the
        already-correctly-streamed content as the real final answer with
        no "retry" needed at all). When it DOES replace: unlike a normal
        retry_output round, there's no next `agent` call coming to supply
        fresh tokens, so this case ALSO synthesizes one "token" event
        carrying retry_exhausted's own replacement text right after the
        "retry" event — two real bugs, caught live in immediate
        succession: first, without that synthesis, a rejected answer's
        own tokens (already streamed before the graph decided to replace
        them — e.g. a leaked system prompt) reached the client with no
        correction ever following, while the checkpointed state correctly
        held the honest fallback; second, firing "retry" UNCONDITIONALLY
        (the first fix's own initial shape) would have told the client to
        discard a TRUSTED, already-correct answer too, with no
        replacement message to follow it — a blank draft despite a
        perfectly good checkpointed answer. A THIRD source, same
        replace-in-place shape: `on_chain_end` of `check_output` itself,
        when `_insert_missing_citation_markers` mechanically added a
        missing `[n]` marker to an already-streamed answer — the exact
        same "tokens already reached the client before the graph edited
        them" problem retry_exhausted's own replacement solves, so it
        gets the identical fix (clear, "retry", synthesize the corrected
        text as one "token" event) rather than a new event type. Also
        ONLY when check_output actually returned a replacement — the
        overwhelmingly common "nothing needed correcting" case emits
        nothing here.
      {"type": "compacted"} — NOT terminal: graph.py's compact_history
        (GRAPH_PATTERNS.md pattern 41) just trimmed older turns out of
        active context (folding them into state["history_summary"] and
        leaving a permanent breadcrumb message behind — see
        _compaction_marker_message). Purely informational for a client
        (nothing to clear, unlike "retry" — this fires before `agent`'s
        own call even starts, so no tokens have streamed yet this turn) —
        lets a UI show a transient "summarizing older messages" status
        instead of the turn just going quiet for however long that LLM
        call takes. Fired from `on_chain_end` of the `compact_history`
        node, but ONLY when it actually trimmed something — that node
        runs on every single turn, and returns `{}` on most of them.
      {"type": "citations", "items": [...]} — emitted right before "done",
        only when the answer actually cited something; the same
        state["used_citations"] shape graph_routing.py's check_output computes.
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

            if (
                kind == "on_chat_model_stream"
                and event.get("metadata", {}).get("langgraph_node") == "agent"
                and not event.get("metadata", {}).get("subagent_name")
            ):
                # Real bug, caught live via Langfuse: astream_events emits
                # on_chat_model_stream for EVERY chat-model call anywhere in
                # the graph, not just the main answer — suggest_followups
                # (pattern 27) and compact_history (pattern 41) each make
                # their OWN separate llm.invoke() call, and without this
                # filter their output streamed as "token" events too,
                # concatenating straight onto the end of the real answer
                # with no separator (verified directly: a real turn's
                # displayed text ended with the answer's last citation
                # marker immediately followed by suggest_followups' own
                # generated questions, no space, no newline). `metadata.
                # langgraph_node` (verified empirically against a real
                # astream_events run) is exactly which node's own graph
                # step a given chat-model event belongs to — "agent" is
                # the ONLY node whose text is ever meant to reach a user
                # this way; suggest_followups' output already reaches the
                # client correctly, and separately, via its own dedicated
                # "followups" event below.
                #
                # `metadata.subagent_name` guards a SECOND, distinct source
                # of the same problem: app/agent/tools.py::_run_subagent_impl
                # threads this turn's own callbacks into a NESTED graph's
                # invoke() (so its internal LLM/tool calls trace correctly as
                # child spans) — and that nested graph, built via this exact
                # same build_graph(), ALSO has a node literally named
                # "agent". Without this guard its own internal reasoning
                # tokens would satisfy the langgraph_node check above too and
                # leak into the client's main answer stream, indistinguishable
                # from the top-level answer. Verified empirically: a bare
                # top-level on_chat_model_start already carries a non-empty
                # `parent_ids` (LangGraph's own __start__/channel-write
                # machinery nests everything 2+ levels deep), so
                # `parent_ids`-emptiness is NOT a usable discriminator here —
                # `metadata.subagent_name` (stamped only on the nested run's
                # own config, tools.py's `nested_config["metadata"]`) is.
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
                    # This tool call happened INSIDE a run_subagent's own
                    # nested run (tagged via nested_config["metadata"]),
                    # rather than at the top level — surfaced to the client
                    # so a ~45s delegation isn't a silent black box, tagged
                    # so the UI can render it as the subagent's own activity
                    # rather than confusing it with a top-level tool call.
                    payload["subagent"] = subagent_name
                yield payload

            elif kind == "on_tool_end":
                payload = {"type": "tool_end", "tool": event["name"]}
                subagent_name = event.get("metadata", {}).get("subagent_name")
                if subagent_name:
                    payload["subagent"] = subagent_name
                yield payload

            elif kind == "on_chain_end" and event["name"] == "retry_output":
                # check_output rejected the last answer (too short, or
                # citing without attribution — graph.py's retry_output) and
                # the graph is looping back to `agent` for a fresh attempt.
                # Every token already streamed above belongs to the
                # REJECTED answer — with no signal here, a client just
                # keeps appending (verified directly: app/api/static/
                # index.html's handleEvent does exactly that), so the
                # rejected answer and the retried one render concatenated
                # as if they were one continuous response, no separator, no
                # indication a retry happened at all. `final_answer` is
                # reset for the SAME reason on the trace side — it's what
                # ends up as Langfuse's own `output` field below.
                final_answer.clear()
                yield {"type": "retry"}

            elif kind == "on_chain_end" and event["name"] == "check_output":
                # check_output (graph.py) mechanically inserted a missing
                # citation marker into the answer AFTER it already
                # finished streaming above (_insert_missing_citation_
                # markers) — same "already-streamed tokens belong to
                # stale content" problem retry_exhausted's own in-place
                # replacement below solves, fixed the identical way: clear
                # the client's buffer via the same "retry" event, then
                # synthesize a fresh "token" event for the CORRECTED text,
                # since — like retry_exhausted, unlike a normal
                # retry_output round — no next `agent` call is coming to
                # supply it. Only fires when check_output actually
                # returned a replacement; the overwhelmingly common case
                # where nothing needed correcting emits nothing here, same
                # guard retry_exhausted's own handler below uses.
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
                # graph.py's route_after_check gave up on a stuck
                # retry_output loop (MAX_CONSECUTIVE_SAME_RETRY_REASON) —
                # retry_exhausted is TERMINAL (no next agent round coming,
                # unlike retry_output above) and, critically,
                # UNCONDITIONALLY replaces the last message rather than
                # trusting it (see that node's own docstring on why —
                # it's specifically content check_output already judged
                # bad on repeat, e.g. a leaked system prompt).
                #
                # Real bug, caught immediately after shipping
                # leaks_system_prompt detection: the REJECTED content
                # still streamed live via on_chat_model_stream same as any
                # normal answer (nothing about being "about to be
                # replaced" stops the model's own tokens from reaching the
                # client as they're generated) — so by the time this event
                # fires, `final_answer` already holds that full rejected
                # text, which means the "no on_chat_model_stream fired"
                # fallback further below (guarded on `final_answer` being
                # EMPTY) never triggers, and retry_exhausted's own
                # corrected replacement message silently never reached the
                # client at all. Observed live: a system-prompt leak
                # streamed to the user in full, TWICE (once per retry
                # round), with no correction ever shown, while the
                # CHECKPOINTED state correctly held the honest fallback —
                # the exact content this whole mechanism exists to keep
                # off the wire. Fixed the same way retry_output's own
                # "retry" event already tells the client to discard
                # what's rendered so far, plus synthesizing a fresh
                # "token" event for the REAL replacement text here, since
                # (unlike retry_output) there's no next agent round that
                # would otherwise supply it.
                #
                # ONLY when the node actually replaced something, though:
                # retry_exhausted (graph.py) no-ops (`{}`) for
                # too_short/uncited — reasons it TRUSTS the repeatedly-
                # rejected content and leaves it as the real final answer
                # (see _TRUST_CONTENT_RETRY_REASONS) — and that content
                # already streamed correctly via on_chat_model_stream on
                # its own round. A second real bug, caught immediately
                # while fixing the first: firing "retry" unconditionally
                # here would tell the client to discard that ALREADY-
                # CORRECT content anyway, and with no replacement message
                # to follow it (empty `output`), the client would be left
                # with a blank draft despite the checkpointed state
                # holding a perfectly good answer.
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
                # compact_history runs on EVERY turn but only actually does
                # something once history_summary/token count trip its
                # ceiling — `output` is `{}` (graph.py's own early return)
                # on every turn it doesn't, so check for real work
                # (a "messages" key means it built RemoveMessage stubs
                # plus a breadcrumb, see graph.py's own docstring) rather
                # than firing this unconditionally.
                output = event["data"].get("output") or {}
                if output.get("messages"):
                    yield {"type": "compacted"}

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
    except asyncio.CancelledError:
        # A genuine asyncio-level cancellation reaching here — NOT the
        # same thing as TurnCancelled above, which only fires from
        # cancel_check's own cooperative Redis-flag poll at a clean event
        # boundary. This is the real `task.cancel()` machinery: the ASGI
        # layer tearing down a disconnected request's task, an
        # asyncio.wait_for elsewhere timing out and cancelling this task
        # as a side effect, etc. Since Python 3.8, CancelledError is a
        # BaseException (not Exception) SPECIFICALLY so a bare `except
        # Exception` like the one below can't accidentally swallow it —
        # which means without this branch, a real cancellation skipped
        # straight past both except clauses, never called trace.update()
        # or _record_turn_metrics, and this generator just died with no
        # clean terminal event. The only thing that recorded anything was
        # Langfuse's own CallbackHandler, wired independently into
        # graph.astream_events()'s own callbacks — verified live against a
        # real load-test run: "LangGraph" spans left permanently open (no
        # end_time, ever) with status_message set to raw asyncio
        # internals ("<Task cancelled name=... coro=<AsyncExitStack.
        # __aexit__()...>>"), not any message this app ever wrote.
        #
        # Re-raises after cleanup — a cancellation is real and must still
        # propagate (the caller, e.g. app/turns/agent_worker.py's own
        # dispatch loop, needs to see it), this branch only makes sure
        # the trace/metrics get a clean, honest record before it does.
        if trace:
            trace.update(
                output="".join(final_answer) + " [cancelled: task cancelled]",
                level="WARNING",
            )
        _record_turn_metrics(time.monotonic() - start, "cancelled")
        metrics.agent_streaming_cancellation_total.inc()
        raise
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
        # check state.next afterwards. aget_state, not get_state: this
        # async function runs directly on the checkpointer's own event
        # loop (via
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
            used_citations = state.values.get("used_citations") or []
            ungrounded_claims_count = state.values.get("ungrounded_claims_count") or 0
            followups = state.values.get("followups") or []
            if not final_answer:
                # No on_chat_model_stream events fired this turn — every
                # node that produces a final AIMessage WITHOUT calling the
                # chat model (reject_input, reject_context,
                # reject_moderation, context_window_exceeded, a
                # semantic-cache HIT (pattern 22), or the no_answer safety
                # net) hits this: `final_answer` only ever accumulates from
                # token-streaming events, so a turn that never streamed
                # anything left the caller with nothing but a bare "done"
                # and no way to learn what the answer actually was — a
                # real, previously-undiscovered bug, caught live against a
                # cached "hi" response that streamed only {"type": "done"}
                # despite a real cached answer sitting in
                # state.values["messages"][-1]. Sent as one synthetic
                # "token" event (not a new event type) so every existing
                # client already renders it correctly.
                final_message = state.values["messages"][-1]
                skipped_text = (
                    final_message.content if isinstance(final_message.content, str) else ""
                )
                if skipped_text:
                    # Appended to `final_answer` itself, not just yielded —
                    # a second real bug, caught live via Langfuse (a
                    # no_answer-fallback turn recorded trace.output as ""
                    # even though the client received the real fallback
                    # apology text): `trace.update` below reads
                    # `final_answer`, so without this the trace stays
                    # blank on every turn that takes this branch, showing
                    # a real answer to the actual caller but an empty one
                    # to anyone inspecting the trace.
                    final_answer.append(skipped_text)
                    yield {"type": "token", "content": skipped_text}
            if trace:
                trace.update(output="".join(final_answer))
            _record_turn_metrics(
                time.monotonic() - start,
                _turn_outcome(state.values),
                state.values,
                ctx=(cfg.get("configurable") or {}).get("ctx"),
                thread_id=(cfg.get("configurable") or {}).get("thread_id"),
            )
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
    astream_events_resume(thread_id, approved, ctx) to continue — see
    app/channels/chat.py's `--hitl` mode for a runnable example of driving
    this pause/resume cycle. Default False keeps existing callers
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
        "recursion_limit": RECURSION_LIMIT,
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
    That's app/turns/agent_worker.py (the Redis Streams queue's consumer
    side, GRAPH_PATTERNS.md pattern 43): a request pulled off a shared
    queue has no round trip back to whoever originally asked, so it must
    resolve any pause on its own rather than leave the run paused forever —
    and app/channels/telegram.py, which has no inline-keyboard approve/reject
    UX of its own (see its module docstring).

    One-round auto-decline, not a loop (pattern 8): a SECOND pause on the
    same turn (e.g. a rejected action followed by another mutating attempt)
    is left to the model's own next response.
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
    the streaming counterpart to a plain `graph.invoke(Command(resume=approved),
    config)` call. `thread_id` must match the paused turn.

    `ctx` is required and re-supplied here, not reused from the original
    pause: `config["configurable"]` does NOT persist across a resume
    automatically (verified empirically) — whatever's resolving the
    approval is expected to re-assert who they are, the same way the
    original request had to. This is also where a stricter check (e.g.
    "the resuming principal must match the tenant that paused") would
    naturally go if this app ever needed one; today `resumability_error_async`
    below only checks the checkpoint's build compatibility, not the
    resumer's identity against the pauser's.

    Opens its own Langfuse trace rather than extending the original one: a
    human's approval can take arbitrarily long (minutes, hours), so this is
    modeled as a separate trace correlated by session_id, not one span kept
    open across the wait. That same "arbitrarily long" wait is exactly why
    resumability_error_async is checked here: a human might approve hours (or
    a redeploy) after the pause, so this is the one entry point most likely
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
        "recursion_limit": RECURSION_LIMIT,
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
    cfg = {"configurable": {"thread_id": thread_id, "ctx": ctx}, "recursion_limit": RECURSION_LIMIT}
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

    Returns `[{"role": "user"|"assistant"|"system", "text": str}, ...]` —
    the first two are the same roles the web UI already renders bubbles
    for. Deliberately narrower than the full persisted state:
    `SystemMessage`s (the ephemeral history-summary/context injections are
    never actually persisted back to state, but a seeded base prompt is)
    and `ToolMessage`s (tool call/result pairs) are both omitted — this is
    a replay of the conversational back-and-forth a user saw, not a full
    forensic trace (that's what Langfuse is for); an AIMessage with no
    text of its own (a pure tool-calling turn — the model's visible
    answer came on a LATER message once the tool result came back) is
    skipped the same way the live UI never rendered an empty bubble for it.

    The ONE exception to "SystemMessages are omitted": a compact_history
    breadcrumb (graph.py's `_compaction_marker_message`, tagged via
    `COMPACTION_MARKER_KEY`) comes back as `role: "system"` — unlike the
    seeded base prompt (internal plumbing, never meant for a human to
    read), this one exists specifically so a session's replay shows that
    older turns were cut, not just silently fewer turns than actually
    happened.
    """
    graph = await init_graph_async()
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
            "recursion_limit": RECURSION_LIMIT,
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
