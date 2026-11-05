"""The `BudgetHoldStore` adapter (`app/ports/cost.py`) over the appdata Postgres: the `tenant_budget_holds` table
(postgres-init/16-tenant-budget-holds.sql).

Statements only. Whether a hold is taken at all, what an unreachable table means (the turn proceeds, unprotected, and a metric counts it)
and when a hold is stale are the application's (`app/agent/cost/budget_holds.py`); this module raises on any database error and lets the
caller decide. Every statement is scoped by tenant.
"""
from app.core.sql_store import get_connection


class PgBudgetHoldStore:
    async def reserve(self, tenant: str, hold_id: str, amount_usd: float, *, stale_after_minutes: int) -> None:
        # The sweep of this tenant's abandoned holds shares the insert's transaction (`get_connection` is one, see core/sql_store.py).
        # It is housekeeping, not correctness: the read already ignores stale holds.
        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM tenant_budget_holds "
                "WHERE tenant = %s AND created_at <= now() - make_interval(mins => %s)",
                (tenant, stale_after_minutes),
            )
            await conn.execute(
                "INSERT INTO tenant_budget_holds (hold_id, tenant, reserved_usd) VALUES (%s, %s, %s)",
                (hold_id, tenant, amount_usd),
            )

    async def release(self, tenant: str, hold_id: str) -> None:
        async with get_connection() as conn:
            await conn.execute(
                "DELETE FROM tenant_budget_holds WHERE hold_id = %s AND tenant = %s",
                (hold_id, tenant),
            )

    async def in_flight(self, tenant: str, *, stale_after_minutes: int) -> float:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0) FROM tenant_budget_holds "
                "WHERE tenant = %s AND created_at > now() - make_interval(mins => %s)",
                (tenant, stale_after_minutes),
            )
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
