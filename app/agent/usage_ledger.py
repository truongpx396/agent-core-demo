"""Real usage/cost ledger (pattern 26) — closes the "no hollow Meter" gap:
a shipped default should keep an actual ledger, not a no-op that makes a
broken deployment look configured (same principle as moderation.py's real
check).

Persisted in the same `appdata` Postgres database as sql_store.py
(`usage_ledger` table, postgres-init/03-meter.sql). Every row is
tenant+principal scoped.

Cost is NOT computed here. The agent node prices every LLM call as it happens
(app/agent/pricing.py: LiteLLM's per-model input/output/cached rates) and keeps a
running `total_cost_usd`; the caller hands that figure to `record_usage`. One
number therefore feeds the in-run ceiling, the ledger and the tenant allowance,
and they cannot disagree. An unpriced model records $0 here — loudly: see
pricing.py and `agent_unpriced_usage_total` — never as a quiet default.
"""
import logging
import uuid
from datetime import datetime

from app.agent.model_resolver import resolve_model
from app.agent.sql_store import get_connection
from app.core import metrics
from app.core.config import USAGE_LEDGER_MIN_RETENTION_DAYS
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# A hold older than this is treated as abandoned (a worker that died mid-turn
# without ever reaching release_budget_reservation) rather than real in-flight
# spend — see postgres-init/16-tenant-budget-holds.sql for why each turn's hold
# carries its own timestamp instead of one running total per tenant. Generous
# relative to REQUEST_TIMEOUT_SECONDS (60s default) for the same reason
# app/job_queue/queue.py's THREAD_LOCK_TTL_SECONDS is: a "resume" job's own
# turn isn't wrapped in that same timeout, so a legitimate hold can outlive it.
# A turn that really runs longer simply stops being counted — the fail-open
# direction this module takes everywhere.
RESERVATION_STALE_AFTER_MINUTES = 5


async def record_usage(
    ctx: SecurityCtx | None,
    thread_id: str,
    model_alias: str,
    total_tokens: int,
    cost_usd: float,
) -> None:
    """Best-effort write-through after a turn completes
    (`runtime.py::_record_turn_metrics`). A failing write must not fail
    the turn it's recording — same degrade-don't-crash posture as
    `semantic_cache.py::set()`. No-ops without a valid ctx or with zero
    tokens (unattributable / nothing to meter). `cost_usd` is the turn's
    running total from the agent node (see the module docstring); a negative
    figure is clamped to 0 so a bad price can never credit a tenant.
    """
    if not valid_ctx(ctx) or total_tokens <= 0:
        return
    cost_usd = max(cost_usd, 0.0)
    # The resolved CONCRETE model behind `model_alias` (GRAPH_PATTERNS.md
    # pattern 38) — None if resolution itself degrades (LiteLLM
    # unreachable, alias unknown); never blocks the write.
    resolved_model = await resolve_model(model_alias)
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO usage_ledger "
                "(tenant, principal, thread_id, model_alias, total_tokens, cost_usd, resolved_model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    ctx["tenant"],
                    ctx["principal"],
                    thread_id,
                    model_alias,
                    total_tokens,
                    cost_usd,
                    resolved_model,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - a failing write must not fail the turn it records; counted and alerted instead
        # Not just a log line: this is committed spend that never reached the
        # ledger the tenant allowance is read from, so the allowance undercounts
        # until someone notices (alert LedgerWriteFailing, spec 008 A1).
        metrics.agent_cost_governance_degraded_total.labels(path="ledger_write").inc()
        logger.warning(
            "usage ledger write failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


async def usage_summary(
    tenant: str, principal: str | None = None, since: datetime | None = None
) -> dict:
    """Total tokens and cost for `tenant`, optionally narrowed to one
    `principal` and/or to usage on/after `since`. `since` is what
    `_tenant_over_daily_budget` (runtime.py) uses for a ROLLING 24h window
    (`now - 24h`, not calendar-day boundaries, so a tenant's near-limit
    status never resets mid-day). `since=None` (all-time) is the right
    default for `GET /usage`'s "total ever spent" question instead."""
    where = ["tenant = %s"]
    params: list = [tenant]
    if principal:
        where.append("principal = %s")
        params.append(principal)
    if since is not None:
        where.append("recorded_at >= %s")
        params.append(since)

    sql = (
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
        f"FROM usage_ledger WHERE {' AND '.join(where)}"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        total_tokens, total_cost = await cur.fetchone()
    return {"total_tokens": int(total_tokens), "total_cost_usd": float(total_cost)}


async def reserve_budget(ctx: SecurityCtx | None, amount_usd: float) -> str | None:
    """Records a hold of `amount_usd` against `ctx`'s tenant — one row for this
    turn, with its own timestamp — called right before a turn that passed
    `_tenant_over_daily_budget`'s check actually starts spending. Closes the gap
    between "checked" and "recorded": this turn's own cost isn't in
    usage_ledger yet (record_usage only runs after it completes), so without a
    hold a sibling turn racing the same tenant would see the SAME stale "spent
    so far" and pass the check too. Always paired with
    `release_budget_reservation` once the turn ends (success, failure, or
    timeout) — callers use a `finally` for that, same shape as
    `app/job_queue/queue.py::acquire_thread_lock`'s own release contract.

    Returns the hold's id — hand it back to `release_budget_reservation` — or
    None if nothing was reserved (invalid ctx, non-positive amount, or the write
    failed). Best-effort, same fail-open posture as every other write in this
    module: a reservation failure must not block a turn, it just means this one
    turn's spend goes unprotected against a concurrent sibling, exactly the
    pre-reservation behavior.

    The sweep and the insert share one transaction (`get_connection` is one —
    see sql_store.py). The sweep removes this tenant's abandoned holds so the
    table doesn't grow; the read already ignores them, so it is housekeeping,
    not correctness."""
    if not valid_ctx(ctx) or amount_usd <= 0:
        return None
    hold_id = str(uuid.uuid4())
    try:
        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM tenant_budget_holds "
                "WHERE tenant = %s AND created_at <= now() - make_interval(mins => %s)",
                (ctx["tenant"], RESERVATION_STALE_AFTER_MINUTES),
            )
            await conn.execute(
                "INSERT INTO tenant_budget_holds (hold_id, tenant, reserved_usd) VALUES (%s, %s, %s)",
                (hold_id, ctx["tenant"], amount_usd),
            )
        return hold_id
    except Exception as exc:  # noqa: BLE001 - fail open: a reservation failure must not block a turn; counted instead
        metrics.agent_cost_governance_degraded_total.labels(path="reservation").inc()
        logger.warning(
            "tenant_budget_reservation_failed", extra={"error_class": type(exc).__name__}
        )
        return None


async def release_budget_reservation(ctx: SecurityCtx | None, hold_id: str | None) -> None:
    """Deletes the hold `reserve_budget` returned. Deleting by id (scoped to the
    tenant, like every other statement here) means a release can only ever undo
    its own hold: a duplicate or late release is a no-op rather than a second
    subtraction, and it can never reach into another turn's amount. The old
    running-total design needed a `GREATEST(..., 0)` clamp to hide exactly that
    class of drift; there is nothing left to clamp."""
    if not valid_ctx(ctx) or not hold_id:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM tenant_budget_holds WHERE hold_id = %s AND tenant = %s",
                (hold_id, ctx["tenant"]),
            )
    except Exception as exc:  # noqa: BLE001 - a failed release only leaves a hold that ages out on its own; counted instead
        metrics.agent_cost_governance_degraded_total.labels(path="reservation").inc()
        logger.warning("tenant_budget_release_failed", extra={"error_class": type(exc).__name__})


async def in_flight_reservation(tenant: str) -> float:
    """Current reserved-but-not-yet-recorded spend for `tenant`: the sum of its
    holds younger than RESERVATION_STALE_AFTER_MINUTES. A hold abandoned by a
    worker that died mid-turn stops counting on ITS OWN clock — whatever else
    the tenant is doing — rather than permanently inflating this tenant's
    apparent spend, with no background cleanup job. (An earlier single-row
    design let the next reserve resurrect an abandoned amount and let a busy
    tenant's activity keep it alive indefinitely; see
    postgres-init/16-tenant-budget-holds.sql.) Fails open to 0.0 on any error,
    same posture as every other read this module/runtime.py's own budget check
    already has: a reservation-table outage must not ALSO take down the ledger
    check that's still healthy."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0) FROM tenant_budget_holds "
                "WHERE tenant = %s AND created_at > now() - make_interval(mins => %s)",
                (tenant, RESERVATION_STALE_AFTER_MINUTES),
            )
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception as exc:  # noqa: BLE001 - fail open to 0.0 (ledger-only check); counted instead
        metrics.agent_cost_governance_degraded_total.labels(path="reservation").inc()
        logger.warning(
            "tenant_budget_in_flight_read_failed", extra={"error_class": type(exc).__name__}
        )
        return 0.0


# Rows go in batches so one sweep never holds a long lock or one huge transaction
# on a ledger that has grown for a year; each batch commits on its own.
SWEEP_BATCH_SIZE = 5000
# One run's ceiling: 1,000 batches is 5 million rows at the default size, far past a year's
# backlog on any single deployment this was sized for. The app never writes a row older than
# the cutoff, so a sweep ends at the first short batch; the ceiling is for the day something
# does (a restore or backfill re-inserting old rows) so one run still has a bounded duration.
SWEEP_MAX_BATCHES = 1000


async def sweep_old_rows(
    *,
    older_than_days: int,
    batch_size: int = SWEEP_BATCH_SIZE,
    max_batches: int = SWEEP_MAX_BATCHES,
) -> int:
    """Deletes ledger rows recorded more than `older_than_days` ago and returns
    how many (spec 008 A3: nothing ever trimmed this table, and the allowance
    read runs before every turn). An operator job (`scripts/usage_ledger_sweep.py`),
    never reachable from a request, so unlike every other statement in this module
    it deliberately spans tenants — retention is a property of the table, not of one
    tenant's data.

    The caller must keep `older_than_days` above the longest window any budget
    reads (a 31-day month), or a window would silently stop counting spend that is
    still inside it: anything under `USAGE_LEDGER_MIN_RETENTION_DAYS` raises here,
    before a row is touched, so the floor holds for every caller and not just the
    script.

    The inner SELECT has no ORDER BY on purpose: rows are inserted in time order, so
    the oldest sit first in the heap and the scan finds a full batch quickly, whereas
    sorting a year of rows to delete the oldest few thousand would cost more than the
    delete.

    A run deletes at most `max_batches * batch_size` rows. Hitting that is logged
    (`usage_ledger_sweep_hit_batch_ceiling`) and is not an error: the job is
    idempotent, so the next run continues where this one stopped.
    """
    if older_than_days < USAGE_LEDGER_MIN_RETENTION_DAYS:
        raise ValueError(
            f"older_than_days={older_than_days} is below the {USAGE_LEDGER_MIN_RETENTION_DAYS}-day floor: "
            "a monthly budget window would stop counting spend that is still inside it"
        )
    total = 0
    for _ in range(max_batches):
        async with get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM usage_ledger WHERE id IN ("
                "SELECT id FROM usage_ledger "
                "WHERE recorded_at < now() - make_interval(days => %s) LIMIT %s)",
                (older_than_days, batch_size),
            )
            deleted = cur.rowcount
        total += deleted
        if deleted < batch_size:
            return total
    logger.warning(
        "usage_ledger_sweep_hit_batch_ceiling",
        extra={"deleted": total, "max_batches": max_batches, "batch_size": batch_size},
    )
    return total
