"""Exactly-once guard for every "mutating"/"outward" tool (TOOL_CAPABILITIES, app/agent/tools/builtin.py). It
closes the gap `app/job_queue/agent_worker.py`'s reclaim loop left open: a reclaimed `"resume"` job re-invokes
whichever tool calls were pending at its `human_approval` pause under the SAME `tool_call_id`s (the pause
happens before those calls run, and `Command(resume=...)` continues that exact checkpointed state). Without
this, retrying one risked re-sending an email, re-creating a ticket, or repeating any other already-applied
side effect.

Persisted in the same `appdata` Postgres database as core/sql_store.py (`tool_call_dedup`,
postgres-init/13-tool-call-dedup.sql), keyed by `tool_call_id` alone: the LLM provider assigns a fresh one per
tool call it decides to make, so two invocations sharing an id are, by construction, the SAME logical
invocation attempted again, never two intended actions colliding.

Scope, deliberately narrow: this makes ONE invocation exactly-once. It does NOT make a whole crashed "turn"
safe to retry through a fresh `astream_events_turn`, which re-asks the LLM and gets brand-new tool_call_ids
that dedup here can't connect to the crashed attempt. `agent_worker.py` instead reclaims a crashed "turn" by
CONTINUING its checkpointed run (`astream_events_continue_turn`, `graph.astream_events(None, ...)`), the same
reasoning generalized from "resume" to any crashed turn (see `_classify_reclaimed_turn` and
`astream_events_continue_turn`).

Fails OPEN on its own storage failure, with the same posture and reason as `app/agent/cost/budget_holds.py`
and `app/agent/runtime/sessions.py` (see tests/conftest.py's `mock_appdata_postgres`): a dedup-store outage
must degrade to "run the tool, unprotected" rather than block every mutating/outward tool call. This is
defense in depth for a rare compounding failure (a crash-recovery retry landing exactly while the dedup store
is also down), never a precondition for a tool call.

A second, DIFFERENT gap that keying on `tool_call_id` can't close: `fn()`'s soft timeout (`arun_with_timeout`)
firing AFTER its write already committed on the far side but before this module's closing `UPDATE`. The retry
arrives under a BRAND NEW tool_call_id (a fresh LLM decision) that no id-keyed defense can recognize as "the
same one again"; `MutatingToolTimedOut` handles it by steering the agent to verify, not by deduplicating after
the fact.
"""
import logging
from collections.abc import Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from app.core import metrics
from app.core.security import SecurityCtx
from app.core.sql_store import get_connection

logger = logging.getLogger(__name__)


def _thread_id_from_config(config: RunnableConfig | None) -> str | None:
    return (config or {}).get("configurable", {}).get("thread_id")


class MutatingToolTimedOut(Exception):
    """Raised by `idempotent()` in place of the `TimeoutError` `fn()` itself raised, when `fn()` is a
    mutating/outward tool's own `arun_with_timeout(...)` call.

    It needs its own type because `arun_with_timeout`'s docstring already names the risk: `asyncio.wait_for`
    cancels the AWAITING task, not necessarily whatever `fn()` awaits underneath, so the real side effect (an
    INSERT, a Qdrant upsert) can commit on the far side of a slow store even though this call reports failure.
    That is a DIFFERENT window from the crash-replay race the `idempotent`/`_claim_or_cached_result`
    docstrings discuss (the SAME tool_call_id seen twice): the agent sees a plain tool failure and, if it
    retries, does so under a BRAND NEW tool_call_id that `tool_call_dedup`'s id-keyed claim has never seen, so
    no hardening of it can catch a second real side effect.

    `graph/utils.py::friendly_tool_error` special-cases this type to steer the agent toward checking whether
    the effect already landed (a matching list/status/read tool) instead of blindly retrying: the one general
    defense that works for any mutating/outward tool, with no per-tool business key.
    """

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(
            f"{tool_name} timed out — its side effect may already have been applied"
        )


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
    """Run `fn()` (a tool's own real `arun_with_timeout(...)` call) at most once per `tool_call_id`, ever. A
    second call with an id already seen returns the FIRST call's result without running `fn` again, so the
    side effect (an email, a ticket, a note) happens once however many times the call is replayed.

    Mechanics: an unconditional `INSERT ... ON CONFLICT DO NOTHING` on `tool_call_id` (the primary key) is the
    atomic claim. Whichever caller's INSERT lands owns running `fn`; every other caller sees no row inserted
    and reads back what is there.

    Narrow accepted race: if the row read back has `result IS NULL` (the winner's `fn()` is still running, or
    died before the closing UPDATE), this caller runs `fn()` itself rather than blocking or looping, so a
    double run is possible in that exact window. Accepted because nothing today can put two callers there
    concurrently: the only real caller (`app/job_queue/agent_worker.py`'s reclaim path) retries a job only
    well after `AGENT_WORKER_RECLAIM_IDLE_SECONDS` of the ORIGINAL attempt going silent (presumed dead, not
    concurrently running), and `queue.py::acquire_thread_lock` already rules out two jobs for the same
    thread_id, hence the same tool_call_id. A future caller invoking this concurrently would need to close
    that window (block-and-poll, or a proper `SELECT ... FOR UPDATE`).
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

    try:
        result = await fn()
    except TimeoutError as exc:
        # Only fn()'s own soft timeout is re-raised as this distinguishable
        # type (see MutatingToolTimedOut's own docstring) — any other
        # exception from fn() means the tool genuinely failed before doing
        # anything, so the plain, generic error message is already correct
        # for it.
        raise MutatingToolTimedOut(tool_name) from exc
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


async def sweep_stale_rows(*, older_than_hours: int) -> int:
    """Deletes `tool_call_dedup` rows older than `older_than_hours`.
    Meant for a periodic cron sweep (`scripts/tool_call_dedup_sweep.py`),
    never the hot path `idempotent()` itself runs.

    postgres-init/13-tool-call-dedup.sql creates this table with no
    retention of its own: every mutating/outward tool call ever made
    accumulates here, full result text included, forever, without this.
    Only ever needs to outlive `AGENT_WORKER_RECLAIM_IDLE_SECONDS` (the
    longest a genuine crash-recovery replay can lag behind the original
    attempt) by a wide margin — the sweep script's own default is generous
    accordingly. Returns the number of rows deleted.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "DELETE FROM tool_call_dedup WHERE created_at < now() - make_interval(hours => %s)",
            [older_than_hours],
        )
        return cur.rowcount
