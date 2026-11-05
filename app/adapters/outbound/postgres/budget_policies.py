"""The `BudgetPolicyStore` adapter (`app/ports/cost.py`) over the appdata Postgres: the `budget_policies` table
(postgres-init/18-budget-policies.sql).

Statements only. The cache, the validation of a write (`period`, a negative limit) and the posture on a failed read are the application's
(`app/agent/cost/budget_policies.py`, `budgets.check`). A table that does not exist is the one database error this adapter translates, into
`PolicyTableMissing`: it means the migration has not been applied, and the application reads it as "no overrides" with one warning.
"""
from psycopg import errors as pg_errors

from app.core.sql_store import get_connection
from app.ports.cost import ALL_PRINCIPALS, TENANT_SUBJECT, Override, PolicyTableMissing


def _override(row) -> Override:
    subject, period, limit = row
    return Override(subject, period, None if limit is None else float(limit))


class PgBudgetPolicyStore:
    async def for_person(self, tenant: str, principal: str) -> list[Override]:
        try:
            async with get_connection() as conn:
                cur = await conn.execute(
                    "SELECT subject, period, limit_usd FROM budget_policies "
                    "WHERE tenant = %s AND subject IN (%s, %s, %s)",
                    (tenant, TENANT_SUBJECT, ALL_PRINCIPALS, principal),
                )
                rows = await cur.fetchall()
        except pg_errors.UndefinedTable as exc:
            raise PolicyTableMissing from exc
        return [_override(row) for row in rows]

    async def for_tenant(self, tenant: str) -> list[Override]:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT subject, period, limit_usd FROM budget_policies WHERE tenant = %s ORDER BY subject, period",
                (tenant,),
            )
            rows = await cur.fetchall()
        return [_override(row) for row in rows]

    async def upsert(self, tenant: str, subject: str, period: str, limit_usd: float | None, updated_by: str) -> None:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO budget_policies (tenant, subject, period, limit_usd, updated_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant, subject, period) DO UPDATE "
                "SET limit_usd = EXCLUDED.limit_usd, updated_by = EXCLUDED.updated_by, updated_at = now()",
                (tenant, subject, period, limit_usd, updated_by),
            )

    async def delete(self, tenant: str, subject: str, period: str) -> bool:
        async with get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM budget_policies WHERE tenant = %s AND subject = %s AND period = %s",
                (tenant, subject, period),
            )
            return cur.rowcount > 0
