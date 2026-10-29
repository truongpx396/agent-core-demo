"""The `UsageEventStore` adapter (`app/ports/cost.py`) over the appdata Postgres: the `usage_events` table
(postgres-init/19-usage-events.sql), the agent's meter and the spend its caps sum.

Statements only. What to write, when to skip, what a failed write costs the turn (nothing: it is counted) and how an event is priced are the
application's (`app/agent/cost/usage_events.py`). The table's missing migration is the one database error translated, into `UsageTableMissing`.

`insert` hands a newly written event to billing in the SAME transaction, through `app.billing_integration.port`: the record, the debit and
the export row are atomic with the event, which is what stops a replay from double-debiting (`packages/billing/docs/PATTERNS.md`). That
call needs the transaction's connection, so it cannot happen anywhere but beside the INSERT; the application never sees a connection.
"""
from datetime import datetime
from decimal import Decimal

from psycopg import errors as pg_errors

from app.billing_integration import port as billing_port
from app.core.sql_store import get_connection
from app.ports.cost import (
    EventsByTenantDay,
    UsageEventRow,
    UsageTableMissing,
    UsageTotals,
    Window,
)

_COLUMNS = (
    "event_id, tenant, principal, thread_id, kind, model_alias, resolved_model, "
    "input_tokens, output_tokens, cached_input_tokens, total_tokens, cost_usd, "
    "price_input_per_token, price_output_per_token"
)
_VALUES = (
    "%(event_id)s, %(tenant)s, %(principal)s, %(thread_id)s, %(kind)s, "
    "%(model_alias)s, %(resolved_model)s, %(input_tokens)s, %(output_tokens)s, "
    "%(cached_input_tokens)s, %(total_tokens)s, %(cost_usd)s, "
    "%(price_input_per_token)s, %(price_output_per_token)s"
)
_INSERT = f"INSERT INTO usage_events ({_COLUMNS}) VALUES ({_VALUES}) ON CONFLICT (event_id) DO NOTHING"

_ALLOW_DELETE = "SELECT set_config('usage_events.allow_delete', 'on', true)"

# No ORDER BY, on purpose: events are inserted in time order, so the oldest sit first in the heap and the scan finds a full batch
# quickly, whereas sorting a year of rows to delete the oldest few thousand would cost more than the delete.
_BATCH = """
WITH doomed AS (
    SELECT e.event_id FROM usage_events e
    WHERE e.occurred_at < now() - make_interval(days => %(days)s)
    LIMIT %(n)s
), gone AS (
    DELETE FROM usage_events e USING doomed d WHERE e.event_id = d.event_id RETURNING 1
)
SELECT count(*) FROM gone
"""


async def _insert(row: UsageEventRow) -> bool:
    """True if a row was written, False if `event_id` was already there. The statement relies on the table's PRIMARY KEY for `ON CONFLICT`,
    which a fake cursor cannot prove: the real-Postgres test (tests/integration/test_usage_events_real_postgres.py) does.

    A newly written row is handed to billing in the same transaction; a duplicate is not."""
    async with get_connection() as conn:
        cur = await conn.execute(_INSERT, row)
        inserted = cur.rowcount == 1
        if inserted:
            await billing_port.record_usage_in(
                conn,
                billing_port.Usage(
                    tenant=row["tenant"], event_id=row["event_id"], kind=row["kind"], cost_usd=row["cost_usd"],
                    model=row["resolved_model"] or row["model_alias"], tokens=row["total_tokens"],
                ),
            )
        return inserted


class PgUsageEventStore:
    async def insert(self, row: UsageEventRow) -> bool:
        try:
            return await _insert(row)
        except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn) as exc:
            raise UsageTableMissing from exc

    async def totals(self, tenant: str, principal: str | None = None, since: datetime | None = None) -> UsageTotals:
        where = ["tenant = %s"]
        params: list = [tenant]
        if principal:
            where.append("principal = %s")
            params.append(principal)
        if since is not None:
            where.append("occurred_at >= %s")
            params.append(since)

        sql = (
            "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
            f"FROM usage_events WHERE {' AND '.join(where)}"
        )
        async with get_connection() as conn:
            cur = await conn.execute(sql, params)
            total_tokens, total_cost = await cur.fetchone()
        return UsageTotals(int(total_tokens), float(total_cost))

    async def delete_older_than(self, *, days: int, limit: int) -> int:
        async with get_connection() as conn:
            await conn.execute(_ALLOW_DELETE)
            cur = await conn.execute(_BATCH, {"days": days, "n": limit})
            (gone,) = await cur.fetchone()
        return int(gone)

    async def by_tenant_day(self, window: Window) -> EventsByTenantDay:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT tenant, (occurred_at AT TIME ZONE 'UTC')::date, COALESCE(SUM(cost_usd), 0), COUNT(*), COUNT(*) FILTER (WHERE cost_usd IS NULL) "
                "FROM usage_events WHERE occurred_at >= %s AND occurred_at <= %s GROUP BY 1, 2",
                (window.start, window.end),
            )
            rows = await cur.fetchall()
        return EventsByTenantDay(
            usd={(t, d): Decimal(usd) for t, d, usd, _, _ in rows},
            unpriced={(t, d): unpriced for t, d, _, _, unpriced in rows if unpriced},
            events=sum(count for _, _, _, count, _ in rows),
        )

    async def known_tenants(self) -> set[str]:
        # The table's index leads with the tenant, so this is an index-only scan; a few passes a day cost nothing next to the window scan.
        async with get_connection() as conn:
            cur = await conn.execute("SELECT DISTINCT tenant FROM usage_events")
            return {row[0] for row in await cur.fetchall()}
