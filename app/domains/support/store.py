"""Fixed, parameterized queries against `support_tickets`
(postgres-init/07-support-tickets.sql) — the support copilot's own system
of record. Same discipline as `app/agent/sql_store.py::query_employees`: no
generated SQL, every query explicitly `WHERE tenant = %s`, reusing that
module's own pooled `appdata` connection (`get_connection()`) rather than
opening a second pool to the same database.
"""
from app.agent.sql_store import get_connection


def create_ticket(
    tenant: str, requester: str, subject: str, description: str, priority: str
) -> int:
    """Insert one new ticket, always `status='open'` — the only way a
    ticket comes into existence, mirroring app/agent/tools.py::add_note's
    "a fresh id, never a caller-targeted one" discipline. Returns the new
    ticket's id."""
    sql = (
        "INSERT INTO support_tickets (tenant, requester, subject, description, priority) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, requester, subject, description, priority])
        (ticket_id,) = cur.fetchone()
        return int(ticket_id)


def get_ticket(tenant: str, ticket_id: int) -> dict | None:
    """One ticket, scoped to `tenant` — a ticket id from another tenant
    returns None, never that tenant's row (the same "narrows, never
    widens" boundary app/core/security.py's doc_ids scoping already
    establishes for Qdrant)."""
    sql = (
        "SELECT id, tenant, requester, subject, description, priority, status, "
        "escalation_reason, created_at, updated_at FROM support_tickets "
        "WHERE tenant = %s AND id = %s"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, ticket_id])
        columns = [desc.name for desc in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row, strict=True)) if row else None


def escalate_ticket(tenant: str, ticket_id: int, reason: str) -> bool:
    """Marks a ticket escalated — returns False (no update applied) if no
    ticket with that id exists for this tenant, so the tool impl can tell
    the model "no such ticket" instead of silently no-op-ing."""
    sql = (
        "UPDATE support_tickets SET status = 'escalated', escalation_reason = %s, "
        "updated_at = now() WHERE tenant = %s AND id = %s"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [reason, tenant, ticket_id])
        return cur.rowcount > 0
