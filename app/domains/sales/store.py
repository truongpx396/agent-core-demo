"""Fixed, parameterized queries against `crm_leads`/`crm_followups`
(postgres-init/08-crm.sql) — the sales concierge's own system of record.
Same discipline as app/agent/sql_store.py/app/domains/support/store.py: no
generated SQL, every query explicitly `tenant = %s`, reusing
app/agent/sql_store.py's own pooled `appdata` connection.
"""
from datetime import datetime

from app.agent.sql_store import get_connection


def find_or_create_lead(tenant: str, name: str, contact: str, note: str) -> int:
    """One lead per (tenant, contact) — postgres-init/08-crm.sql's unique
    index makes this a real upsert, not a check-then-insert race: a second
    interaction with the same contact appends to `notes` (a running log,
    newest last) rather than creating a duplicate lead or discarding the
    earlier history."""
    sql = """
        INSERT INTO crm_leads (tenant, name, contact, notes)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant, contact) DO UPDATE
            SET notes = crm_leads.notes || E'\\n' || EXCLUDED.notes,
                updated_at = now()
        RETURNING id
    """
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, name, contact, note])
        (lead_id,) = cur.fetchone()
        return int(lead_id)


def set_lead_status(tenant: str, contact: str, status: str) -> bool:
    sql = "UPDATE crm_leads SET status = %s, updated_at = now() WHERE tenant = %s AND contact = %s"
    with get_connection() as conn:
        cur = conn.execute(sql, [status, tenant, contact])
        return cur.rowcount > 0


def get_lead(tenant: str, contact: str) -> dict | None:
    sql = (
        "SELECT id, tenant, name, contact, status, notes, created_at, updated_at "
        "FROM crm_leads WHERE tenant = %s AND contact = %s"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, contact])
        columns = [desc.name for desc in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row, strict=True)) if row else None


def add_followup(tenant: str, contact: str, due_at: datetime, note: str, created_by: str) -> int | None:
    """Schedules a follow-up against the lead for `contact` — returns None
    (no follow-up created) if no lead with that contact exists yet for
    this tenant, so the tool impl can tell the model to log the
    interaction first."""
    lead = get_lead(tenant, contact)
    if lead is None:
        return None
    sql = (
        "INSERT INTO crm_followups (tenant, lead_id, due_at, note, created_by) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, lead["id"], due_at, note, created_by])
        (followup_id,) = cur.fetchone()
        return int(followup_id)


def list_pending_followups(tenant: str, contact: str | None = None) -> list[dict]:
    """Pending follow-ups, most-imminent first, joined with their lead's
    name — like `due_followups` but not bounded to "due by now": this is
    what `list_pending_followups` shows a rep the whole upcoming queue
    (optionally narrowed to one lead's contact) rather than only what a
    cron sweep would act on right now."""
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
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def mark_lead_lost(tenant: str, contact: str, reason: str) -> bool:
    """Closes out a lead that isn't going to convert: sets `status='lost'`,
    appends `reason` to `notes` (same running-log append `find_or_create_lead`
    already uses), and cancels any of its still-pending follow-ups so
    scripts/followup_sweep.py's cron sweep never nudges a rep about a dead
    lead. Returns False (nothing updated) if no lead with that contact
    exists for this tenant, same "tell the model, don't silently no-op"
    contract as `set_lead_status`."""
    lead = get_lead(tenant, contact)
    if lead is None:
        return False
    with get_connection() as conn:
        conn.execute(
            "UPDATE crm_leads SET status = 'lost', "
            "notes = COALESCE(notes || E'\\n', '') || %s, updated_at = now() "
            "WHERE tenant = %s AND contact = %s",
            [reason, tenant, contact],
        )
        conn.execute(
            "UPDATE crm_followups SET status = 'cancelled' "
            "WHERE tenant = %s AND lead_id = %s AND status = 'pending'",
            [tenant, lead["id"]],
        )
    return True


def due_followups(tenant: str, as_of: datetime) -> list[dict]:
    """Pending follow-ups due by `as_of`, joined with their lead's name/
    contact — what scripts/followup_sweep.py sweeps. Scoped to `tenant`
    even though today's only caller (the cron sweep) runs per-tenant
    itself; keeping the tenant predicate here too means a future caller
    (e.g. a multi-tenant sweep) can't accidentally drop it."""
    sql = """
        SELECT f.id, f.due_at, f.note, l.contact, l.name AS lead_name
        FROM crm_followups f
        JOIN crm_leads l ON l.id = f.lead_id
        WHERE f.tenant = %s AND f.status = 'pending' AND f.due_at <= %s
        ORDER BY f.due_at
    """
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, as_of])
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def mark_followup_done(tenant: str, followup_id: int) -> None:
    sql = "UPDATE crm_followups SET status = 'done' WHERE tenant = %s AND id = %s"
    with get_connection() as conn:
        conn.execute(sql, [tenant, followup_id])


def lead_history(tenant: str, contact: str) -> dict | None:
    """A lead's full record plus its follow-ups (pending and done) —
    what package_lead_brief assembles a handoff brief from."""
    lead = get_lead(tenant, contact)
    if lead is None:
        return None
    sql = (
        "SELECT id, due_at, note, status FROM crm_followups "
        "WHERE tenant = %s AND lead_id = %s ORDER BY due_at"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [tenant, lead["id"]])
        columns = [desc.name for desc in cur.description]
        followups = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return {**lead, "followups": followups}
