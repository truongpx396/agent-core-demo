"""The in-flight budget holds: what keeps N concurrent turns from all passing the same stale cap check.

A turn that passed `budgets.check` reserves `MAX_COST_USD_PER_TURN` for its duration (`reserve_budget`) and
releases it when it ends (`release_budget_reservation`, always, in a `finally`); a sibling turn for the tenant
adds the tenant's in-flight total (`in_flight_reservation`) to what the usage events say was spent. Without
the hold, N turns racing one tenant all read the same stale "spent so far" and all proceed. It is about
concurrency, not history: a hold is a row in `tenant_budget_holds` (postgres-init/16) with its own timestamp,
and one that outlives its turn ages out on its own clock.

This left `usage_ledger.py` when that module went (specs/010 T030c3; the caps sum `spend.py`, retention is
`usage_events_retention.py`). The `usage_ledger` TABLE stays as read-only history that
`scripts/usage_events_carry_over.py` can copy; dropping it is a later destructive migration.

Every function fails OPEN (a hold that can't be written or read must not block a turn) and counts the failure
(`agent_cost_governance_degraded_total{path="reservation"}`).
"""
import logging
import uuid

from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx
from app.core.sql_store import get_connection

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


async def reserve_budget(ctx: SecurityCtx | None, amount_usd: float) -> str | None:
    """Hold `amount_usd` against `ctx`'s tenant for one turn, taken just before it starts spending.

    Closes the gap between "checked" and "recorded": this turn's remaining calls aren't events yet, so without
    a hold a sibling turn racing the same tenant reads nearly the same "spent so far" and passes too. Always
    paired with `release_budget_reservation` in a `finally`, as with
    `app/job_queue/queue.py::acquire_thread_lock`.

    Returns the hold id, or None if nothing was reserved (invalid ctx, non-positive amount, write failed).
    Best-effort and fail-open like every write here: a failure just leaves this turn unprotected against a
    concurrent sibling.

    The sweep of this tenant's abandoned holds shares the insert's transaction (`get_connection` is one, see
    core/sql_store.py). It is housekeeping, not correctness: the read already ignores stale holds.
    """
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
