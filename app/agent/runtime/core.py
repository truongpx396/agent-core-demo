"""Shared agent runtime, run by `app/adapters/inbound/workers/agent_worker.py` and nothing else that serves chat.

It centralises the compiled graph (singleton), per-thread system-prompt seeding and Langfuse callbacks, so
memory and tracing behave the same whichever front door a turn came through: the CLI (`app/adapters/inbound/channels/cli/chat.py`) and
Telegram (`app/adapters/inbound/channels/telegram/bot.py`) are clients of the FastAPI service, which publishes every turn to the queue,
and a worker runs them here.

## Durable checkpointing (init_graph_async)

A durable saver (the `Checkpointing` port, `app/ports/outbound/checkpointing.py`; Postgres in production), not `build_graph()`'s
default `MemorySaver`: a paused `human_approval` gate is only a real safety control if it survives a redeploy, and it
must be safe under concurrent access from several processes sharing the same `thread_id`s (FastAPI + `agent_worker.py`
replicas).

The Postgres saver binds its async lock to the event loop it was *created* on; calling it from another loop
raises "bound to a different event loop" (verified). So `init_graph_async()` opens the checkpointer on the
CALLING loop, and every process driving the graph must await it on that loop before any graph call, never from
a background thread or a different loop.

## One graph per domain

A process may serve several domains (`agent_worker.py`, `AGENT_WORKER_DOMAINS`). Each gets its own compiled graph
(its manifest and tools) over ONE shared checkpointer: `PgCheckpointing.open()` makes a new pool on every call, so
opening one per domain would orphan all but the last. Which graph a turn runs on is `bind_domain(name)`, set by the
worker around each job; a task started inside it inherits it (`ContextVar`). Outside any `bind_domain` (the API's
session reads, the tests) it is `DEFAULT_DOMAIN`.

This file owns the checkpointer/graph lifecycle (`init_graph_async`/`close_checkpointer_pool`), the
session-directory upsert (`upsert_session`) and the tenant daily-budget bindings
(`_check_allowance`/`allowance_refusal`; the rule is `app/agent/cost/budgets.py`). They are kept together
because each reads or reassigns a module global (`_graphs`/`_saver`, or config names several tests
monkeypatch on this module) and that only works with consumer and binding in one file. The streaming entry
points live in `app/agent/runtime/stream.py`, which reaches this module's globals as `runtime_module.X` rather
than a bare import, for the same monkeypatch reason.
"""
import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.agent.cost import budget_holds, budgets, pricing
from app.agent.graph.build import build_graph
from app.agent.graph.state import MAX_ITERATIONS
from app.agent.runtime import sessions
from app.core.config import (
    BUDGET_CHECK_FAILURE_POLICY,
    CHAT_MODEL,
    MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
    MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
    MAX_COST_USD_PER_TENANT_PER_DAY,
    MAX_COST_USD_PER_TENANT_PER_MONTH,
    MAX_COST_USD_PER_TURN,
)
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.domains import domain_catalog

logger = logging.getLogger(__name__)

_graphs: dict[str, Any] = {}  # compiled graph per domain name
_saver = None  # the one checkpointer every domain's graph shares
_graph_lock = asyncio.Lock()  # guards the two above while they are built: without it concurrent first
# callers each open a checkpointer pool and overwrite the graph, leaking every loser's pool (the same guard
# as core/sql_store.py::_get_pool). Every production caller primes sequentially, so this is hardening.
DEFAULT_DOMAIN = "ecorp"  # the domain a turn runs on when nothing bound one
_bound_domain: ContextVar[str | None] = ContextVar("agent_domain", default=None)


def current_domain() -> str:
    return _bound_domain.get() or DEFAULT_DOMAIN


@contextmanager
def bind_domain(name: str) -> Iterator[None]:
    """Runs everything inside on `name`'s graph and stamps its sessions with `name`."""
    token = _bound_domain.set(name)
    try:
        yield
    finally:
        _bound_domain.reset(token)


# LangGraph's own graph-step cap — coarser than MAX_ITERATIONS (an agent-node
# count): each turn also runs several fixed pre/post-loop nodes, and each
# agent<->tools round trip is 2 steps. A flat "12" undercounted for a model
# making several real tool-call round trips (hit GraphRecursionError on an
# ordinary question before MAX_ITERATIONS was reached — same bug fixed for the
# subagent graph, see GRAPH_PATTERNS.md pattern 46). Derived, with margin, so
# it can't drift out of sync with MAX_ITERATIONS.
RECURSION_LIMIT = MAX_ITERATIONS * 2 + 15


async def _check_allowance(ctx: SecurityCtx | None) -> budgets.Allowance:
    """May this caller start another turn? The rule lives in `app/agent/cost/budgets.py`; this binds the
    configured limits at CALL time (not import time), so the module globals below stay the
    single place a test or a deployment re-points them."""
    return await budgets.check(
        ctx,
        defaults=budgets.Defaults(
            tenant_day=MAX_COST_USD_PER_TENANT_PER_DAY,
            tenant_month=MAX_COST_USD_PER_TENANT_PER_MONTH,
            principal_day=MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
            principal_month=MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
        ),
        fail_policy=BUDGET_CHECK_FAILURE_POLICY,
    )


async def allowance_refusal(ctx: SecurityCtx | None) -> ErrorEnvelope | None:
    """The error to emit INSTEAD of starting a turn, or None if it may start. Every entry
    point that begins model work asks this one question (a new turn, and a resume), so the
    caller never has to tell "over budget" from "could not be checked" itself."""
    allowance = await _check_allowance(ctx)
    return budgets.refusal_envelope(allowance) if allowance.refused else None


async def chat_model_refused_as_unpriced() -> bool:
    """True if a turn must be refused because the chat model has no known price
    and `UNPRICED_MODEL_POLICY` is "block". Checked before any model work, and
    before the tenant allowance: every dollar ceiling multiplies tokens by a
    price, so with none they all read $0 and "within budget" means nothing. The
    price is cached (pricing.get_price), so this costs no round trip per turn."""
    return await pricing.refuse_unpriced(CHAT_MODEL)


def model_unpriced_envelope() -> ErrorEnvelope:
    return ErrorEnvelope(
        code=ErrorCode.MODEL_UNPRICED,
        message=(
            "This deployment cannot meter the configured model's cost, so it is not "
            "serving requests. This is a configuration problem on our side."
        ),
    )


async def reserve_turn_budget(ctx: SecurityCtx | None) -> str | None:
    """Called once a turn has passed `allowance_refusal` and is
    about to actually run — holds MAX_COST_USD_PER_TURN (the hard cap
    graph/routing.py::should_continue already enforces per turn, so it's
    always a safe upper bound on what this turn could cost) against this
    tenant's in-flight total. Returns the hold's id, or None if `ctx` is
    invalid or the write itself failed — callers pass this straight to
    `release_turn_budget` in a `finally`, so a failed reservation and a real
    one both round-trip correctly (releasing None is a no-op)."""
    return await budget_holds.reserve_budget(ctx, MAX_COST_USD_PER_TURN)


async def release_turn_budget(ctx: SecurityCtx | None, hold_id: str | None) -> None:
    """Reverses `reserve_turn_budget` — called unconditionally once a
    turn ends (success, failure, or timeout), never only on success: the
    reservation's whole job is to cover the WINDOW while this turn is
    running, not to track whether it actually succeeded (the usage events,
    written as each call returned, are the real accounting for a turn)."""
    await budget_holds.release_budget_reservation(ctx, hold_id)


async def upsert_session(ctx: SecurityCtx | None, thread_id: str, text: str) -> None:
    """Record/refresh this thread_id in the session directory (the conversation
    switcher, app/agent/runtime/sessions.py) — called at the START of every turn, unlike
    the turn metrics which only fire on a completed turn. Deliberate: a
    rejected/moderated/short-circuited turn is still a real conversation and
    should still appear in "switch conversation." Degrades to a no-op on its
    own failure (can't fail the turn).

    The row is stamped with `current_domain()`, the domain whose graph runs the turn.
    """
    await sessions.upsert_session(ctx, thread_id, text, domain=current_domain())


async def close_checkpointer_pool() -> None:
    """Closes what the checkpointer holds open cleanly (skipping this leaves background worker tasks/connections
    open at process exit). Called from graceful-shutdown paths only (app/adapters/inbound/api/main.py's lifespan,
    agent_worker.py::run); a no-op if the checkpointer was never opened."""
    await checkpointing.get().close()


async def init_graph_async(domain: str | None = None):
    """The compiled graph for `domain` (default: `current_domain()`), built on first use over the shared
    checkpointer, which this opens on the CALLING loop (see the module docstring). `agent_worker.py` primes
    every domain it serves at start, so no turn pays the build and an unknown name fails at boot; every
    stream entry point calls it without an argument and gets the bound domain's graph.

    An unknown `domain` raises `ValueError` (the catalog lists the known names).
    """
    global _saver
    name = domain or current_domain()
    graph = _graphs.get(name)
    if graph is not None:
        return graph
    async with _graph_lock:
        graph = _graphs.get(name)  # re-check: another caller may have built it while this one waited for the lock
        if graph is None:
            manifest, plugin = domain_catalog.get().resolve(name)
            if _saver is None:
                _saver = await checkpointing.get().open()
            graph = _graphs[name] = build_graph(manifest, plugin, checkpointer=_saver)
    return graph
