"""Fixed, parameterized queries against `support_tickets`
(postgres-init/07-support-tickets.sql, 09-support-ticket-notes.sql) —
the support copilot's system of record. No generated SQL, every query
explicitly `WHERE tenant = %s`, reusing sql_store's pooled `appdata`
connection.
"""
from app.agent.sql_store import get_connection


async def create_ticket(
    tenant: str, requester: str, subject: str, description: str, priority: str
) -> int:
    """Insert one new ticket, always `status='open'`. Returns the new
    ticket's id (fresh, never caller-targeted)."""
    sql = (
        "INSERT INTO support_tickets (tenant, requester, subject, description, priority) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, requester, subject, description, priority])
        (ticket_id,) = await cur.fetchone()
        return int(ticket_id)


async def get_ticket(tenant: str, ticket_id: int) -> dict | None:
    """One ticket, scoped to `tenant` — a ticket id from another tenant
    returns None, never that tenant's row."""
    sql = (
        "SELECT id, tenant, requester, subject, description, priority, status, "
        "escalation_reason, notes, created_at, updated_at FROM support_tickets "
        "WHERE tenant = %s AND id = %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, ticket_id])
        columns = [desc.name for desc in cur.description]
        row = await cur.fetchone()
        return dict(zip(columns, row, strict=True)) if row else None


async def list_tickets_for_requester(tenant: str, requester: str, limit: int = 10) -> list[dict]:
    """A customer's own tickets, most recent first, so they don't need to
    already know a ticket number. Scoped to `tenant` AND `requester` —
    narrower than `get_ticket`'s tenant-only scoping, since there's no
    caller-supplied ticket id to trust here."""
    sql = (
        "SELECT id, subject, priority, status, created_at FROM support_tickets "
        "WHERE tenant = %s AND requester = %s ORDER BY created_at DESC LIMIT %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, requester, limit])
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


async def escalate_ticket(tenant: str, ticket_id: int, reason: str) -> bool:
    """Marks a ticket escalated. Returns False if no ticket with that id
    exists for this tenant, so the tool impl can tell the model "no such
    ticket" instead of silently no-op-ing."""
    sql = (
        "UPDATE support_tickets SET status = 'escalated', escalation_reason = %s, "
        "updated_at = now() WHERE tenant = %s AND id = %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [reason, tenant, ticket_id])
        return cur.rowcount > 0


async def add_comment(tenant: str, ticket_id: int, comment: str) -> bool:
    """Appends a customer follow-up to `notes` (running log, newest last),
    same append shape as `crm_leads.notes`. Returns False if no ticket
    with that id exists for this tenant."""
    sql = (
        "UPDATE support_tickets SET notes = COALESCE(notes || E'\\n', '') || %s, "
        "updated_at = now() WHERE tenant = %s AND id = %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [comment, tenant, ticket_id])
        return cur.rowcount > 0
