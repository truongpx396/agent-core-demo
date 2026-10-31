"""The `SessionStore` adapter (`app/ports/sessions.py`) over the appdata Postgres: the `chat_sessions` table
(postgres-init/06-chat-sessions.sql).

It is not the usage table (that one no-ops on a zero-token or rejected turn and carries no title, so a session with no billable tokens would
vanish from the switcher) and not the checkpointer's `checkpoints` table (no tenant or principal column to scope by, and a title parsed out of
its blob format would be fragile). Statements only: whether a write may fail a turn is the application's call.
"""
from app.core.sql_store import get_connection


class PgSessionStore:
    async def touch(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> None:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                (thread_id, tenant, principal, title, domain),
            )

    async def claim(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> bool:
        # `ON CONFLICT DO NOTHING` makes the claim atomic: of two racers for a new id the loser blocks on the unique index until the
        # winner commits, then sees its row.
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO NOTHING",
                (thread_id, tenant, principal, title, domain),
            )
            cur = await conn.execute(
                "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
                "AND principal = %s AND domain = %s",
                (thread_id, tenant, principal, domain),
            )
            return await cur.fetchone() is not None

    async def owns(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
                "AND principal = %s AND domain = %s",
                (thread_id, tenant, principal, domain),
            )
            return await cur.fetchone() is not None

    async def list(self, tenant: str, principal: str, domain: str) -> list[dict]:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT thread_id, title, created_at, last_active_at FROM chat_sessions "
                "WHERE tenant = %s AND principal = %s AND domain = %s ORDER BY last_active_at DESC",
                (tenant, principal, domain),
            )
            columns = [desc.name for desc in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(columns, row, strict=True)) for row in rows]
