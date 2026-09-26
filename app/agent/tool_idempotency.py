"""Exactly-once guard for every "mutating"/"outward" tool (TOOL_CAPABILITIES,
app/agent/tools.py) — closes the gap `app/job_queue/agent_worker.py`'s
reclaim loop left open: a reclaimed `"resume"` job re-invokes whichever
tool calls were already pending at its `human_approval` pause, under the
SAME `tool_call_id`s (the pause happens before those calls run, and
`Command(resume=...)` continues that exact checkpointed state) — without
this, retrying one risked re-sending an email, re-creating a ticket, or any
other already-applied side effect a SECOND time.

Persisted in the same `appdata` Postgres database as sql_store.py
(`tool_call_dedup` table, postgres-init/13-tool-call-dedup.sql). Keyed by
`tool_call_id` alone: the LLM provider assigns a fresh one per tool call it
decides to make, so two invocations sharing an id are, by construction, the
SAME logical invocation being attempted again — never two different
intended actions colliding.

Scope, deliberately narrow: this makes ONE invocation exactly-once. It does
NOT make a whole crashed "turn" safe to blindly retry from scratch — a
fresh `astream_events_turn` call re-asks the LLM, which gets brand-new
tool_call_ids unrelated to whatever the crashed attempt did, so dedup here
can never "catch" that case. `agent_worker.py::_is_safe_to_retry_turn`
still exists, unchanged, for exactly that reason — see its own docstring.

Fails OPEN on its own storage failure — same posture, and same reason, as
`app/agent/usage_ledger.py`/`app/agent/sessions.py`'s own `get_connection()`
callers (see tests/conftest.py's `mock_appdata_postgres` docstring): a
dedup-store outage must degrade to "run the tool, unprotected" rather than
block every mutating/outward tool call in the app on it. This is a
defense-in-depth layer for a rare compounding failure (a crash-recovery
retry landing exactly while the dedup store is ALSO down), never a
precondition for a tool call to work at all.
"""
import logging
from collections.abc import Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from app.agent.sql_store import get_connection
from app.core import metrics
from app.core.security import SecurityCtx

logger = logging.getLogger(__name__)


def _thread_id_from_config(config: RunnableConfig | None) -> str | None:
    return (config or {}).get("configurable", {}).get("thread_id")


async def _claim_or_cached_result(tool_call_id: str, ctx: SecurityCtx, config: RunnableConfig, tool_name: str) -> str | None:
    """`None` means THIS caller won the claim and must run `fn()` itself
    (either genuinely first, or the earlier claimant's own row is still
    `result IS NULL` — see `idempotent`'s own docstring on why that narrow
    race is accepted, not closed, here). Any other return value is the
    prior call's own cached result, to hand back unchanged."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO tool_call_dedup (tool_call_id, tenant, thread_id, tool_name) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING "
            "RETURNING tool_call_id",
            [tool_call_id, ctx["tenant"], _thread_id_from_config(config), tool_name],
        )
        claimed = await cur.fetchone() is not None
        if claimed:
            return None
        cur = await conn.execute(
            "SELECT result FROM tool_call_dedup WHERE tool_call_id = %s", [tool_call_id]
        )
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def idempotent(
    *,
    tool_call_id: str,
    ctx: SecurityCtx,
    config: RunnableConfig,
    tool_name: str,
    fn: Callable[[], Awaitable[str]],
) -> str:
    """Runs `fn()` (a tool's own real `_arun_with_timeout(...)` call) at
    most once per `tool_call_id`, ever. A second call with an id already
    seen returns the FIRST call's own result without running `fn` again —
    the actual side effect (an email, a ticket, a note) only ever happens
    once, regardless of how many times this specific tool call gets
    replayed.

    Mechanics: an unconditional `INSERT ... ON CONFLICT DO NOTHING` on
    `tool_call_id` (the primary key) is the atomic claim — whichever
    caller's INSERT actually lands owns running `fn`; every other caller
    sees no row inserted and instead reads back whatever's there.

    Narrow accepted race: if the row read back has `result IS NULL` (the
    winner's `fn()` is still running, or died before ever completing the
    closing UPDATE), this caller runs `fn()` itself too rather than
    blocking or looping — a real double-run is possible in that exact
    window. Accepted because nothing in this app today can actually put two
    callers in that window concurrently: the only real caller of this
    (`app/job_queue/agent_worker.py`'s reclaim path) only ever retries a
    job well after `AGENT_WORKER_RECLAIM_IDLE_SECONDS` of the ORIGINAL
    attempt going silent (presumed dead, not concurrently running), and
    `queue.py::acquire_thread_lock` already rules out two jobs for the same
    thread_id — hence the same tool_call_id — running at once in the first
    place. A future caller invoking this from an actually-concurrent
    context would need to close that window for real (e.g. block-and-poll,
    or a proper `SELECT ... FOR UPDATE`) — not needed for what exists today.
    """
    try:
        cached = await _claim_or_cached_result(tool_call_id, ctx, config, tool_name)
    except Exception as exc:  # noqa: BLE001 - see module docstring: this layer fails open, never the tool call itself
        logger.warning(
            "tool_call_dedup_degraded",
            extra={"tool_call_id": tool_call_id, "tool_name": tool_name, "error_class": type(exc).__name__},
        )
        metrics.agent_tool_dedup_degraded_total.inc()
        return await fn()

    if cached is not None:
        logger.info("tool_call_deduplicated", extra={"tool_call_id": tool_call_id, "tool_name": tool_name})
        return cached

    result = await fn()
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE tool_call_dedup SET result = %s WHERE tool_call_id = %s", [result, tool_call_id]
            )
    except Exception as exc:  # noqa: BLE001 - fn() already succeeded; a failure to CACHE that must not fail the call
        logger.warning(
            "tool_call_dedup_result_not_stored",
            extra={"tool_call_id": tool_call_id, "tool_name": tool_name, "error_class": type(exc).__name__},
        )
        metrics.agent_tool_dedup_degraded_total.inc()
    return result
