"""The in-flight budget holds: what keeps N concurrent turns from all passing the same stale cap check.

A turn that passed `budgets.check` reserves `MAX_COST_USD_PER_TURN` for its duration (`reserve_budget`) and gives it back when it
ends (`release_budget_reservation`, always, in a `finally`); a sibling turn for the same tenant adds the tenant's in-flight total
(`in_flight_reservation`) to what the usage events say was already spent. Without the hold, N turns racing one tenant would all read
the same stale "spent so far", all pass, and all proceed: a check-then-act race. It is about concurrency, not history: a hold is
a row in `tenant_budget_holds` (postgres-init/16) with its own timestamp, and one that outlives its turn ages out on its own clock.

This used to live in `usage_ledger.py`, beside the per-turn ledger the caps read. That module is gone (specs/010 T030c3): nothing
writes the ledger since T030c2, the caps sum the usage events (`app/agent/spend.py`), and its retention sweep was replaced by the
events' (`app/agent/usage_events_retention.py`). The holds had never had anything to do with the ledger except sharing its file.
The `usage_ledger` TABLE stays as read-only history that `scripts/usage_events_carry_over.py` can copy into the events; dropping it
is a later, separate, destructive migration.

Every function here fails OPEN (a hold that cannot be written or read must not block a turn) and counts the failure
(`agent_cost_governance_degraded_total{path="reservation"}`).
"""
import logging
import uuid

from app.agent.sql_store import get_connection
from app.core import metrics
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


async def reserve_budget(ctx: SecurityCtx | None, amount_usd: float) -> str | None:
    """Records a hold of `amount_usd` against `ctx`'s tenant — one row for this
    turn, with its own timestamp — called right before a turn that passed
    `budgets.check_tenant_daily`'s check actually starts spending. Closes the gap
    between "checked" and "recorded": the calls this turn has yet to
    make aren't in usage_events yet (each call's event lands when the call returns), so without a
    hold a sibling turn racing the same tenant would see nearly the SAME "spent
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
