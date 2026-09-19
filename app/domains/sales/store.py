"""Fixed, parameterized queries against `crm_leads`/`crm_followups`
(postgres-init/08-crm.sql) — the sales concierge's own system of record.
Same discipline as app/agent/sql_store.py/app/domains/support/store.py: no
generated SQL, every query explicitly `tenant = %s`, reusing
app/agent/sql_store.py's own pooled `appdata` connection.
"""
from datetime import datetime

from app.agent.sql_store import get_connection


async def find_or_create_lead(tenant: str, name: str, contact: str, note: str) -> int:
    """One lead per (tenant, contact) — a real upsert via
    postgres-init/08-crm.sql's unique index, not check-then-insert. A
    second interaction with the same contact appends to `notes` (running
    log, newest last) rather than duplicating the lead."""
    sql = """
        INSERT INTO crm_leads (tenant, name, contact, notes)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant, contact) DO UPDATE
            SET notes = crm_leads.notes || E'\\n' || EXCLUDED.notes,
                updated_at = now()
        RETURNING id
    """
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, name, contact, note])
        (lead_id,) = await cur.fetchone()
        return int(lead_id)


async def set_lead_status(tenant: str, contact: str, status: str) -> bool:
    sql = "UPDATE crm_leads SET status = %s, updated_at = now() WHERE tenant = %s AND contact = %s"
    async with get_connection() as conn:
        cur = await conn.execute(sql, [status, tenant, contact])
        return cur.rowcount > 0


async def get_lead(tenant: str, contact: str) -> dict | None:
    sql = (
        "SELECT id, tenant, name, contact, status, notes, created_at, updated_at "
        "FROM crm_leads WHERE tenant = %s AND contact = %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, contact])
        columns = [desc.name for desc in cur.description]
        row = await cur.fetchone()
        return dict(zip(columns, row, strict=True)) if row else None


async def add_followup(tenant: str, contact: str, due_at: datetime, note: str, created_by: str) -> int | None:
    """Schedules a follow-up for `contact`. Returns None if no lead exists
    yet for this tenant/contact, so the tool impl can tell the model to
    log the interaction first."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return None
    sql = (
        "INSERT INTO crm_followups (tenant, lead_id, due_at, note, created_by) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, lead["id"], due_at, note, created_by])
        (followup_id,) = await cur.fetchone()
        return int(followup_id)


async def list_pending_followups(tenant: str, contact: str | None = None) -> list[dict]:
    """Pending follow-ups, most-imminent first, joined with lead name —
    like `due_followups` but not bounded to "due by now": shows a rep the
    whole upcoming queue, optionally narrowed to one contact."""
    sql = """
        SELECT f.id, f.due_at, f.note, l.contact, l.name AS lead_name
        FROM crm_followups f
        JOIN crm_leads l ON l.id = f.lead_id
        WHERE f.tenant = %s AND f.status = 'pending'
    """
    params: list = [tenant]
    if contact is not None:
        sql += " AND l.contact = %s"
        params.append(contact)
    sql += " ORDER BY f.due_at"
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


async def mark_lead_lost(tenant: str, contact: str, reason: str) -> bool:
    """Closes out a lead: sets `status='lost'`, appends `reason` to
    `notes`, and cancels its pending follow-ups so followup_sweep.py's
    cron never nudges a rep about a dead lead. Returns False if no lead
    exists for this tenant/contact."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return False
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE crm_leads SET status = 'lost', "
            "notes = COALESCE(notes || E'\\n', '') || %s, updated_at = now() "
            "WHERE tenant = %s AND contact = %s",
            [reason, tenant, contact],
        )
        await conn.execute(
            "UPDATE crm_followups SET status = 'cancelled' "
            "WHERE tenant = %s AND lead_id = %s AND status = 'pending'",
            [tenant, lead["id"]],
        )
    return True


async def due_followups(tenant: str, as_of: datetime) -> list[dict]:
    """Pending follow-ups due by `as_of`, joined with lead name/contact —
    what scripts/followup_sweep.py sweeps. Scoped to `tenant` even though
    the only caller already runs per-tenant, so a future multi-tenant
    caller can't accidentally drop the predicate."""
    sql = """
        SELECT f.id, f.due_at, f.note, l.contact, l.name AS lead_name
        FROM crm_followups f
        JOIN crm_leads l ON l.id = f.lead_id
        WHERE f.tenant = %s AND f.status = 'pending' AND f.due_at <= %s
        ORDER BY f.due_at
    """
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, as_of])
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


async def mark_followup_done(tenant: str, followup_id: int) -> None:
    sql = "UPDATE crm_followups SET status = 'done' WHERE tenant = %s AND id = %s"
    async with get_connection() as conn:
        await conn.execute(sql, [tenant, followup_id])


async def append_lead_note(tenant: str, contact: str, note: str) -> bool:
    """Appends `note` to an existing lead's running notes log, for a
    caller that already knows the lead exists (e.g.
    tools.py::enrich_lead_from_website appending a crawled summary).
    Returns False if no lead exists for this tenant/contact."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return False
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE crm_leads SET notes = COALESCE(notes || E'\\n', '') || %s, "
            "updated_at = now() WHERE tenant = %s AND contact = %s",
            [note, tenant, contact],
        )
    return True


async def lead_history(tenant: str, contact: str) -> dict | None:
    """A lead's full record plus its follow-ups (pending and done) —
    what package_lead_brief assembles a handoff brief from."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return None
    sql = (
        "SELECT id, due_at, note, status FROM crm_followups "
        "WHERE tenant = %s AND lead_id = %s ORDER BY due_at"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, lead["id"]])
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        followups = [dict(zip(columns, row, strict=True)) for row in rows]
    return {**lead, "followups": followups}
