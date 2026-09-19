"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the streaming entry points every front-end actually
calls — `astream_events_turn`/`astream_events_resume` for a caller with a real
interactive human on the other end (the CLI, `app/job_queue/agent_worker.py`'s
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
more independently-scaled `app/job_queue/agent_worker.py` processes, all attaching
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
CLI, inside `asyncio.run()`; app/job_queue/agent_worker.py;
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

This file holds the checkpointer/compiled-graph singleton lifecycle
(`init_graph_async`/`close_checkpointer_pool`/`_open_checkpointer`/
`_resolve_domain_name`), per-thread system-prompt seeding
(`_ensure_seeded_async`), the session-directory upsert
(`_upsert_session`), and the tenant daily-budget check
(`_tenant_over_daily_budget`/`_tenant_budget_envelope`) — kept here rather
than split out further because each reads or reassigns a module global
(`_graph`/`_domain_name`/`_seeded`, or `MAX_COST_USD_PER_TENANT_PER_DAY`/
`CHECKPOINTER_DATABASE_URL` as bare names several tests monkeypatch
directly on THIS module) that only works correctly while consumer and
binding live in the same file. The actual streaming entry points
(`astream_events_turn`/`astream_events_turn_unattended`/
`astream_events_resume`/`cancel_run`/`get_session_messages`, plus the
`astream_events` event-translation core) moved to
`app/agent/runtime_stream.py`, and the alternative
`@asynccontextmanager`-based streaming path moved to
`app/agent/runtime_legacy_stream.py` — both split out purely for file
size, no behavior change from the pre-split single-file version. Both
read the handful of names above through `runtime_module.X` (`from
app.agent import runtime as runtime_module`) rather than a plain
statically-imported bare name, for the same monkeypatch reason — see
`runtime_stream.py`'s own module docstring.
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import MAX_ITERATIONS
from app.agent.graph_build import build_graph
from app.core import metrics
from app.core.config import (
    CHECKPOINTER_DATABASE_URL,
    CHECKPOINTER_POOL_MAX_SIZE,
    MAX_COST_USD_PER_TENANT_PER_DAY,
)
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin

logger = logging.getLogger(__name__)

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


_TENANT_BUDGET_WARNING_FRACTION = 0.8  # log/count once a tenant crosses 80% of its daily cap


async def _tenant_over_daily_budget(ctx: SecurityCtx | None) -> bool:
    """True if `ctx`'s tenant has already spent >= MAX_COST_USD_PER_TENANT_PER_DAY
    over the last rolling 24 hours (app/agent/usage_ledger.py's usage_ledger) — checked
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
    posture as usage_ledger.record_usage's own write path and
    app/retrieval/semantic_cache.py/app/agent/moderation.py's read paths.
    """
    if not valid_ctx(ctx):
        return False
    from app.agent import usage_ledger

    try:
        since = datetime.now(UTC) - timedelta(hours=24)
        spent = (await usage_ledger.usage_summary(ctx["tenant"], since=since))["total_cost_usd"]
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


async def _upsert_session(ctx: SecurityCtx | None, thread_id: str, text: str) -> None:
    """Record/refresh this thread_id in the session directory (item #9's
    switcher, app/agent/sessions.py) — called at the START of every turn
    (astream_events_turn, right after seeding), unlike
    usage_ledger.record_usage/_record_turn_metrics which only fire on a
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
    `"turn"` job runs inside whichever app/job_queue/agent_worker.py POOL picked
    it up, already bound to one AGENT_DOMAIN for its whole life — so the
    session row this stamps always matches the domain that actually ran the
    turn."""
    from app.agent import sessions

    await sessions.upsert_session(ctx, thread_id, text, domain=_domain_name)


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

    Out of the box, `AsyncPostgresSaver` would still serialize every
    checkpoint read/write behind one `asyncio.Lock` per saver instance,
    regardless of `conn`'s type (`langgraph/checkpoint/postgres/aio.py`'s
    `_cursor`: `async with self.lock, _ainternal.get_connection(self.conn)`)
    — measured directly against this app (a real HTTP burst, 50 concurrent
    turns) to serialize checkpoint I/O so hard that per-turn latency grew
    ~5x from N=1 to N=50 while `pg_stat_activity` on the checkpointer DB
    never showed more than 1 query actually active at a time, regardless of
    `max_size`. That lock is only genuinely needed when `conn` is a single
    shared `AsyncConnection` — the library's own comment on that line says
    so ("a connection not in pipeline mode can only be used by one
    thread/coroutine at a time") — but it's applied unconditionally even
    when `conn` is an `AsyncConnectionPool`, whose whole job is safely
    handing out independent connections to concurrent callers. This is a
    confirmed, still-open upstream defect (langchain-ai/langgraph#7259,
    verified against that project's own `main` branch on 2026-09-12 — not
    fixed by upgrading `langgraph-checkpoint-postgres`), with a fix
    (#7269, ~2.7x throughput in its own author's benchmark) written but
    not yet merged. The `saver.lock = asyncio.Semaphore(...)` swap below is
    the same workaround that issue's own commenters verified in production
    (~4x reported) — `Semaphore` supports the same `async with` protocol as
    `Lock`, so it's a same-shape drop-in, just capping concurrent
    checkpoint I/O at the pool's real size instead of hard-serializing to
    1. Remove this once #7269 (or equivalent) ships upstream — re-check
    that issue before any langgraph-checkpoint-postgres version bump, since
    this pokes a private, unversioned attribute of a third-party class.
    `min_size` is fixed at 1 (app/agent/sql_store.py's own appdata pool is
    a separate, differently-sized pool — this one doesn't mirror it).

    `saver.setup()` is idempotent (creates its checkpoints/checkpoint_blobs/
    checkpoint_writes tables on first run only) — safe to call on every
    process start, including every independently-scaled agent_worker.py
    instance."""
    global _checkpointer_pool
    pool = AsyncConnectionPool(
        CHECKPOINTER_DATABASE_URL,
        min_size=1,
        max_size=CHECKPOINTER_POOL_MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True)
    saver = AsyncPostgresSaver(conn=pool)
    if isinstance(saver.conn, AsyncConnectionPool):
        # See this function's own docstring (langchain-ai/langgraph#7259):
        # only the single-AsyncConnection case genuinely needs mutual
        # exclusion here; a pool already hands out independent connections
        # to concurrent callers safely on its own.
        saver.lock = asyncio.Semaphore(CHECKPOINTER_POOL_MAX_SIZE)
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
    (app/api/main.py's lifespan, app/job_queue/agent_worker.py::run); a no-op if
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
    restart, and — since `app/job_queue/agent_worker.py`'s own docstring
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
