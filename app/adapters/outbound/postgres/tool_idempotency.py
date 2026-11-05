"""The `ToolCallLedger` adapter (`app/ports/idempotency.py`) over the appdata Postgres: `tool_call_dedup`
(postgres-init/13-tool-call-dedup.sql), keyed by `tool_call_id` alone. The provider assigns a fresh id per tool call, so two invocations sharing
one are, by construction, the SAME invocation attempted again.

The claim is `INSERT ... ON CONFLICT DO NOTHING RETURNING`: the primary key makes it atomic, and whichever caller's INSERT lands owns running the
tool. Statements only; what a failure of this store means for the tool call is the consumer's (`app/core/tool_idempotency.py`: it fails open).
"""
from app.core.sql_store import get_connection


class PgToolCallLedger:
    async def claim_or_cached(self, tool_call_id: str, tenant: str, thread_id: str | None, tool_name: str) -> str | None:
        async with get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tool_call_dedup (tool_call_id, tenant, thread_id, tool_name) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING "
                "RETURNING tool_call_id",
                [tool_call_id, tenant, thread_id, tool_name],
            )
            claimed = await cur.fetchone() is not None
            if claimed:
                return None
            cur = await conn.execute("SELECT result FROM tool_call_dedup WHERE tool_call_id = %s", [tool_call_id])
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def store_result(self, tool_call_id: str, result: str) -> None:
        async with get_connection() as conn:
            await conn.execute("UPDATE tool_call_dedup SET result = %s WHERE tool_call_id = %s", [result, tool_call_id])

    async def sweep(self, older_than_hours: int) -> int:
        async with get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM tool_call_dedup WHERE created_at < now() - make_interval(hours => %s)",
                [older_than_hours],
            )
            return cur.rowcount
