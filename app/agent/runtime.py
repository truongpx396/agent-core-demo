"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises the compiled graph (singleton), per-thread system-prompt seeding,
and Langfuse callbacks, so memory and tracing behave identically regardless
of front-end (CLI, FastAPI, `app/job_queue/agent_worker.py`, `app/channels/telegram.py`).

## Durable checkpointing (init_graph_async)

Uses `AsyncPostgresSaver`, not `build_graph()`'s default `MemorySaver` —
a paused `human_approval` gate is only a real safety control if it survives
a redeploy, and Postgres (unlike a single SQLite file) is safe under
concurrent access from multiple processes sharing the same `thread_id`s
(FastAPI + one or more `agent_worker.py` replicas).

`AsyncPostgresSaver` binds its async lock to whichever event loop it was
*created* on; calling it from a different loop raises "bound to a different
event loop" (verified empirically). `init_graph_async()` therefore opens the
checkpointer on the CALLING loop — every process driving the graph must
await it on that same loop before any graph call, never from a background
thread or a different loop.

This file owns the checkpointer/graph singleton lifecycle
(`init_graph_async`/`close_checkpointer_pool`/`_open_checkpointer`/
`_resolve_domain_name`), per-thread prompt seeding (`_ensure_seeded_async`),
session-directory upsert (`_upsert_session`), and the tenant daily-budget
check (`_tenant_over_daily_budget`/`_tenant_budget_envelope`) — kept together
because each reads/reassigns a module global (`_graph`/`_domain_name`/
`_seeded`, or config names several tests monkeypatch directly on this
module) that only works correctly with consumer and binding in one file.
The streaming entry points live in `app/agent/runtime_stream.py` (and the
legacy `@asynccontextmanager` path in `runtime_legacy_stream.py`) — both
access this module's globals via `runtime_module.X` rather than a bare
import, for the same monkeypatch reason.
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
    MAX_COST_USD_PER_TURN,
)
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin

logger = logging.getLogger(__name__)

_graph = None
_graph_lock = asyncio.Lock()  # guards _graph's construction, not its use — see
# init_graph_async's own docstring: _open_checkpointer()/build_graph() run
# between the `if _graph is None` check and the `_graph = ...` write, so
# without this lock several concurrent first-callers could each pass the
# None check, each open their own checkpointer pool, and each overwrite
# _graph, leaking every loser's pool the same way sql_store.py::_get_pool's
# own identical guard exists to prevent. Every current production caller
# happens to prime this sequentially before any concurrent turn dispatch
# begins (agent_worker.py's run(), telegram.py's poll loop, chat.py's CLI
# loop, FastAPI's lifespan) — so this lock is a hardening, not a fix for a
# live incident, but the pattern is identical to sql_store.py's, which IS
# reachable, so closing both consistently is cheap insurance against a
# future caller (e.g. a direct in-process turn endpoint) reintroducing it.
_domain_name = "ecorp"  # this process's domain (app/domains/registry.py); stamps
# app/agent/sessions.py's chat_sessions.domain column — the graph itself only needs
# the manifest/tools, already baked into _graph by build_graph().
_checkpointer_pool = None  # keeps AsyncConnectionPool alive for the process
# lifetime (GC'ing it closes every pooled connection under the saver); also lets
# close_checkpointer_pool() shut it down explicitly on graceful shutdown.
_seeded: set[str] = set()

# LangGraph's own graph-step cap — coarser than MAX_ITERATIONS (an agent-node
# count): each turn also runs several fixed pre/post-loop nodes, and each
# agent<->tools round trip is 2 steps. A flat "12" undercounted for a model
# making several real tool-call round trips (hit GraphRecursionError on an
# ordinary question before MAX_ITERATIONS was reached — same bug fixed for the
# subagent graph, see GRAPH_PATTERNS.md pattern 46). Derived, with margin, so
# it can't drift out of sync with MAX_ITERATIONS.
RECURSION_LIMIT = MAX_ITERATIONS * 2 + 15


_TENANT_BUDGET_WARNING_FRACTION = 0.8  # log/count once a tenant crosses 80% of its daily cap


async def _tenant_over_daily_budget(ctx: SecurityCtx | None) -> bool:
    """True if `ctx`'s tenant has spent >= MAX_COST_USD_PER_TENANT_PER_DAY over
    the trailing 24h (usage_ledger), checked before a turn starts so an
    over-budget tenant is refused before reaching the LLM/tool loop. Distinct
    from MAX_COST_USD_PER_TURN (graph_routing.py::should_continue), which only
    tracks one turn's own total — this is the cross-turn accumulator.

    `spent` (usage_ledger's own persisted sum) only reflects turns that have
    already COMPLETED and recorded their cost — a sibling turn for the same
    tenant that's already running hasn't landed its row yet, so without
    counting it too, N concurrent turns could all read the same stale
    `spent`, all pass, and all proceed (a check-then-act race, not just a
    theoretical one: astream_events_turn reserves MAX_COST_USD_PER_TURN via
    `_reserve_turn_budget` for the duration of every turn it actually runs,
    specifically so this function can add that in-flight total here and
    catch a burst of concurrent turns the ledger alone would miss).

    Fails OPEN if the ledger read fails, so a ledger outage doesn't also take
    down every turn (same posture as usage_ledger.record_usage and the
    semantic_cache/moderation read paths). `in_flight_reservation` has its
    own independent fail-open (returns 0.0), so a reservation-table hiccup
    degrades this back to the pre-reservation, ledger-only check rather than
    also failing the whole function.
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

    reserved = await usage_ledger.in_flight_reservation(ctx["tenant"])
    projected = spent + reserved

    if projected >= MAX_COST_USD_PER_TENANT_PER_DAY:
        metrics.agent_tenant_budget_exceeded_total.inc()
        return True
    if projected >= _TENANT_BUDGET_WARNING_FRACTION * MAX_COST_USD_PER_TENANT_PER_DAY:
        metrics.agent_tenant_budget_warning_total.inc()
        logger.warning(
            "tenant_approaching_daily_budget",
            extra={
                "tenant": ctx["tenant"],
                "spent_usd": spent,
                "reserved_usd": reserved,
                "limit_usd": MAX_COST_USD_PER_TENANT_PER_DAY,
            },
        )
    return False


def _tenant_budget_envelope() -> ErrorEnvelope:
    return ErrorEnvelope(
        code=ErrorCode.TENANT_BUDGET_EXCEEDED,
        message="This tenant's daily usage budget has been reached. Please try again later.",
    )


async def _reserve_turn_budget(ctx: SecurityCtx | None) -> float:
    """Called once a turn has passed `_tenant_over_daily_budget` and is
    about to actually run — reserves MAX_COST_USD_PER_TURN (the hard cap
    graph_routing.py::should_continue already enforces per turn, so it's
    always a safe upper bound on what this turn could cost) against this
    tenant's in-flight total. Returns the amount actually reserved (0.0 if
    `ctx` is invalid or the reservation write itself failed) — callers pass
    this straight to `_release_turn_budget` in a `finally`, so a failed
    reservation and a real one both round-trip correctly (releasing 0.0 is
    a no-op)."""
    from app.agent import usage_ledger

    if await usage_ledger.reserve_budget(ctx, MAX_COST_USD_PER_TURN):
        return MAX_COST_USD_PER_TURN
    return 0.0


async def _release_turn_budget(ctx: SecurityCtx | None, amount: float) -> None:
    """Reverses `_reserve_turn_budget` — called unconditionally once a
    turn ends (success, failure, or timeout), never only on success: the
    reservation's whole job is to cover the WINDOW while this turn is
    running, not to track whether it actually succeeded (usage_ledger's
    own `record_usage`, called separately, is the real accounting for a
    completed turn)."""
    from app.agent import usage_ledger

    await usage_ledger.release_budget_reservation(ctx, amount)


async def _upsert_session(ctx: SecurityCtx | None, thread_id: str, text: str) -> None:
    """Record/refresh this thread_id in the session directory (the conversation
    switcher, app/agent/sessions.py) — called at the START of every turn, unlike
    usage_ledger's metrics which only fire on a completed turn. Deliberate: a
    rejected/moderated/short-circuited turn is still a real conversation and
    should still appear in "switch conversation." Degrades to a no-op on its
    own failure (can't fail the turn).

    `_domain_name` is this process's own domain (set once in init_graph_async
    alongside `_graph`), so the stamped row always matches whichever domain
    actually ran the turn, regardless of caller.
    """
    from app.agent import sessions

    await sessions.upsert_session(ctx, thread_id, text, domain=_domain_name)


async def _open_checkpointer():
    """Open the AsyncPostgresSaver on whichever loop calls this (see module
    docstring). Stashes the pool in the module global so it isn't
    garbage-collected out from under the connection, and so
    `close_checkpointer_pool()` can shut it down explicitly later.

    Backed by an `AsyncConnectionPool` rather than a single `AsyncConnection`,
    so concurrent turns get independent connections instead of funneling
    through one (psycopg serializes all operations on a single connection).

    Workaround: `AsyncPostgresSaver` still wraps every checkpoint read/write
    in one `asyncio.Lock` per saver instance regardless of `conn` type,
    which hard-serializes checkpoint I/O even with a pool — measured ~5x
    latency growth from N=1 to N=50 concurrent turns. This is an open
    upstream defect (langchain-ai/langgraph#7259; fix #7269 not yet merged).
    Swapping `saver.lock` for an `asyncio.Semaphore` (same `async with`
    protocol) is the community-verified workaround, capping concurrency at
    the pool size instead of serializing to 1. Remove once #7269 ships
    upstream — re-check before any langgraph-checkpoint-postgres bump, since
    this pokes a private attribute of a third-party class.

    `saver.setup()` is idempotent (creates tables on first run only) — safe
    to call on every process start, including each agent_worker.py replica.
    """
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
        # Workaround for langchain-ai/langgraph#7259 — see this function's docstring.
        saver.lock = asyncio.Semaphore(CHECKPOINTER_POOL_MAX_SIZE)
    await saver.setup()
    _checkpointer_pool = pool
    return saver


def _resolve_domain_name(manifest: "AgentManifest | None") -> str:
    """`manifest.name` if given, else build_graph()'s own fallback
    (DEFAULT_MANIFEST, "ecorp") — mirrors build_graph()'s substitution exactly
    so `_domain_name` always names whichever manifest the graph was actually
    built with."""
    if manifest is not None:
        return manifest.name
    from app.agent.manifest import DEFAULT_MANIFEST

    return DEFAULT_MANIFEST.name


async def close_checkpointer_pool() -> None:
    """Closes the checkpointer's connection pool cleanly (skipping this leaves
    background worker tasks/connections open at process exit). Called from
    graceful-shutdown paths only (app/api/main.py's lifespan,
    agent_worker.py::run); a no-op if the checkpointer was never opened."""
    global _checkpointer_pool
    if _checkpointer_pool is not None:
        await _checkpointer_pool.close()
        _checkpointer_pool = None


async def init_graph_async(manifest: "AgentManifest | None" = None, domain: "DomainPlugin | None" = None):
    """Initialize (or reuse) the shared graph for a process with its own
    persistent event loop (FastAPI's lifespan; the CLI's --stream mode).
    Returns the graph; a no-op if the singleton already exists — both
    astream_events_turn/_resume call this directly so the checkpointer ends
    up bound to whichever loop actually drives them, self-healing even if
    startup forgot to prime it via lifespan (`manifest`/`domain` are then
    ignored — the singleton doesn't change domain mid-process).

    `manifest`/`domain` (app/agent/manifest.py) thread straight into
    `build_graph()` and default to `None` (build_graph()'s own Ecorp
    default), so existing callers are unaffected. This is what lets a new
    process boot the same durable-checkpointer machinery against a
    different domain — see app/channels/telegram.py, which resolves
    AGENT_DOMAIN via app/domains/registry.py before priming the singleton.
    Each process still serves exactly one domain for its lifetime.
    """
    global _graph, _domain_name
    if _graph is not None:
        return _graph
    async with _graph_lock:
        if _graph is None:  # re-check: another caller may have finished while this one waited for the lock
            _domain_name = _resolve_domain_name(manifest)
            saver = await _open_checkpointer()
            _graph = build_graph(checkpointer=saver, manifest=manifest, domain=domain)
    return _graph


async def _ensure_seeded_async(graph, thread_id: str) -> None:
    """Seed a new conversation thread with the system prompt exactly once.
    ASYNC only — the streaming entry points run directly on the
    checkpointer's own event loop, where the sync `update_state` raises
    `asyncio.InvalidStateError`; only `aupdate_state` is safe there.

    Reads `graph.manifest.system_prompt` rather than the module-level
    `SYSTEM_PROMPT` constant, so it seeds the correct prompt for whichever
    domain `graph` was actually built for.

    `_seeded` is a same-process fast path, never the source of truth: it's a
    plain in-process `set()`, so it forgets everything on a worker restart,
    and with several `agent_worker.py` replicas a thread's later turns can
    land on a process that never saw this thread_id. Trusting `_seeded`
    alone previously caused a real bug (caught via Langfuse) — a thread's
    system prompt got duplicated, and since `_messages_to_trim` excludes
    SystemMessages from trimming, the duplicate was permanent, quietly
    eating into MAX_TOKENS_PER_TURN forever. Fixed by checking the thread's
    actual persisted state before seeding, and only using `_seeded` to skip
    that check once this process has confirmed it — one extra `aget_state`
    per thread per process, not per turn.
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
