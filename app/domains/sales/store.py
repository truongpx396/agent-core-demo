"""Fixed, parameterized queries against `crm_leads`/`crm_followups`/
`crm_lead_notes` (postgres-init/08-crm.sql, 15-append-notes-as-rows.sql) —
the sales concierge's own system of record. Same discipline as
app/agent/sql_store.py/app/domains/support/store.py: no generated SQL,
every query explicitly `tenant = %s`, reusing app/agent/sql_store.py's own
pooled `appdata` connection.
"""
from datetime import datetime

from app.agent.sql_store import get_connection


async def find_or_create_lead(
    tenant: str, name: str, contact: str, note: str, tool_call_id: str | None = None
) -> int:
    """One lead per (tenant, contact) — a real upsert via
    postgres-init/08-crm.sql's unique index, not check-then-insert. Every
    call also logs `note` as its own row in `crm_lead_notes`
    (postgres-init/15-append-notes-as-rows.sql) — whether this created a
    brand-new lead or found an existing one, the caller is reporting one
    real interaction worth keeping either way. `name` is never updated on
    an existing lead (only `updated_at`), same as before this split —
    it's sticky from whichever call first created the row.

    The lead upsert itself needs no `tool_call_id`: now that notes live in
    their own table, `ON CONFLICT ... DO UPDATE SET updated_at = now()`
    is naturally idempotent on its own — running it twice for the same
    contact converges on the same row regardless (no key needed). Only
    the note insert below does, for the same exactly-once-at-the-row
    reason `create_ticket`'s own docstring explains."""
    sql = """
        INSERT INTO crm_leads (tenant, name, contact)
        VALUES (%s, %s, %s)
        ON CONFLICT (tenant, contact) DO UPDATE SET updated_at = now()
        RETURNING id
    """
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, name, contact])
        (lead_id,) = await cur.fetchone()
        await conn.execute(
            "INSERT INTO crm_lead_notes (tenant, lead_id, note, tool_call_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING",
            [tenant, lead_id, note, tool_call_id],
        )
        return int(lead_id)


async def set_lead_status(tenant: str, contact: str, status: str) -> bool:
    sql = "UPDATE crm_leads SET status = %s, updated_at = now() WHERE tenant = %s AND contact = %s"
    async with get_connection() as conn:
        cur = await conn.execute(sql, [status, tenant, contact])
        return cur.rowcount > 0


async def get_lead(tenant: str, contact: str) -> dict | None:
    """`notes` is computed here, not stored: `STRING_AGG` flattens every
    `crm_lead_notes` row for this lead into the same single newest-last
    string shape the old appended-TEXT-column `notes` used to be (see
    `find_or_create_lead`'s own docstring for why that column became
    rows) — `tools.py::_package_lead_brief_impl` still just reads
    `history["notes"]` unchanged. `LEFT JOIN` (not `INNER`) is what lets a
    lead with zero notes still return a row, with `notes` coming back
    `NULL` — same as the old column's default."""
    sql = (
        "SELECT l.id, l.tenant, l.name, l.contact, l.status, l.created_at, l.updated_at, "
        "STRING_AGG(n.note, E'\\n' ORDER BY n.created_at) AS notes "
        "FROM crm_leads l "
        "LEFT JOIN crm_lead_notes n ON n.lead_id = l.id AND n.tenant = l.tenant "
        "WHERE l.tenant = %s AND l.contact = %s "
        "GROUP BY l.id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [tenant, contact])
        columns = [desc.name for desc in cur.description]
        row = await cur.fetchone()
        return dict(zip(columns, row, strict=True)) if row else None


async def add_followup(
    tenant: str,
    contact: str,
    due_at: datetime,
    note: str,
    created_by: str,
    tool_call_id: str | None = None,
) -> int | None:
    """Schedules a follow-up for `contact`. Returns None if no lead exists
    yet for this tenant/contact, so the tool impl can tell the model to
    log the interaction first.

    `tool_call_id` (postgres-init/14-tool-call-id-columns.sql) — same
    exactly-once-at-the-row shape as support/store.py::create_ticket's own
    docstring: `None` (the default) behaves exactly as before."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return None
    sql = (
        "INSERT INTO crm_followups (tenant, lead_id, due_at, note, created_by, tool_call_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (tool_call_id) DO NOTHING RETURNING id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            sql, [tenant, lead["id"], due_at, note, created_by, tool_call_id]
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
        cur = await conn.execute(
            "SELECT id FROM crm_followups WHERE tool_call_id = %s", [tool_call_id]
        )
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


async def mark_lead_lost(
    tenant: str, contact: str, reason: str, tool_call_id: str | None = None
) -> bool:
    """Closes out a lead: sets `status='lost'`, logs `reason` as a new
    `crm_lead_notes` row, and cancels its pending follow-ups so
    followup_sweep.py's cron never nudges a rep about a dead lead.
    Returns False if no lead exists for this tenant/contact.

    The status/follow-up-cancellation half is naturally idempotent (sets
    a fixed end state — safe to repeat with no key at all); only the
    reason-as-a-note half needs `tool_call_id`, now that it's its own row
    instead of appended inline onto the same UPDATE that used to also
    touch `notes` directly."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return False
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE crm_leads SET status = 'lost', updated_at = now() "
            "WHERE tenant = %s AND contact = %s",
            [tenant, contact],
        )
        await conn.execute(
            "INSERT INTO crm_lead_notes (tenant, lead_id, note, tool_call_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING",
            [tenant, lead["id"], reason, tool_call_id],
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


async def append_lead_note(
    tenant: str, contact: str, note: str, tool_call_id: str | None = None
) -> bool:
    """Logs `note` as a new `crm_lead_notes` row for an existing lead, for
    a caller that already knows the lead exists (e.g.
    tools.py::enrich_lead_from_website appending a crawled summary).
    Returns False if no lead exists for this tenant/contact.

    `tool_call_id` (postgres-init/15-append-notes-as-rows.sql) makes a
    genuine replay under the same id a no-op instead of a second,
    duplicated note — same exactly-once-at-the-row shape as
    `create_ticket`'s own docstring, reachable here now that each note is
    its own row instead of text appended onto a shared column."""
    lead = await get_lead(tenant, contact)
    if lead is None:
        return False
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO crm_lead_notes (tenant, lead_id, note, tool_call_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING",
            [tenant, lead["id"], note, tool_call_id],
        )
        await conn.execute(
            "UPDATE crm_leads SET updated_at = now() WHERE tenant = %s AND contact = %s",
            [tenant, contact],
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
