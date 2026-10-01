"""Fixed, parameterized queries against `support_tickets`/
`support_ticket_comments` (postgres-init/07-support-tickets.sql,
15-append-notes-as-rows.sql) — the support copilot's system of record. No
generated SQL, every query explicitly `WHERE tenant = %s`, reusing
sql_store's pooled `appdata` connection.
"""
from app.agent.sql_store import get_connection


async def create_ticket(
    tenant: str,
    requester: str,
    subject: str,
    description: str,
    priority: str,
    tool_call_id: str | None = None,
) -> int:
    """Insert one new ticket, always `status='open'`. Returns the new
    ticket's id (fresh, never caller-targeted).

    `tool_call_id` (postgres-init/14-tool-call-id-columns.sql), when given,
    makes this call exactly-once at the ROW level via `ON CONFLICT DO
    NOTHING` — closes tool_call_dedup's own accepted "result IS NULL, run
    fn() again" race (see that table's own docstring) one layer down: even
    if `idempotent()` runs `_create_ticket_impl` twice for the SAME
    tool_call_id, only the first INSERT actually lands here; the second
    reads back and returns THAT ticket's id instead of creating a
    duplicate. `None` (the default) behaves exactly as before — no
    conflict target ever matches a NULL column under standard SQL UNIQUE
    semantics, so any caller not passing one is unaffected."""
    sql = (
        "INSERT INTO support_tickets (tenant, requester, subject, description, priority, tool_call_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (tool_call_id) DO NOTHING RETURNING id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            sql, [tenant, requester, subject, description, priority, tool_call_id]
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
        # Lost the race (or genuinely re-ran under the same tool_call_id) —
        # the winning INSERT already committed; hand back ITS id instead of
        # creating a second ticket. tool_call_id is never None here: a
        # conflict can only fire for a real (non-NULL) unique value.
        cur = await conn.execute(
            "SELECT id FROM support_tickets WHERE tool_call_id = %s", [tool_call_id]
        )
        (ticket_id,) = await cur.fetchone()
        return int(ticket_id)


async def get_ticket(tenant: str, ticket_id: int) -> dict | None:
    """One ticket, scoped to `tenant` — a ticket id from another tenant
    returns None, never that tenant's row.

    `notes` is computed here, not stored: `STRING_AGG` flattens every
    `support_ticket_comments` row for this ticket into the SAME single
    newest-last string shape the old appended-TEXT-column `notes` used to
    be (`add_comment`'s own docstring explains why that column became
    rows), so `tools.py::_check_ticket_status_impl` needed no change at
    all — it still just reads `ticket["notes"]`. `GROUP BY t.id` is valid
    with every other `t.*` column un-aggregated because grouping by a
    primary key functionally determines the rest of that same row
    (standard Postgres behavior, not a correctness gap). The `LEFT JOIN`
    (not `INNER`) is what lets a ticket with zero comments still return a
    row, with `notes` coming back `NULL` — same as the old column's
    default."""
    sql = (
        "SELECT t.id, t.tenant, t.requester, t.subject, t.description, t.priority, "
        "t.status, t.escalation_reason, t.created_at, t.updated_at, "
        "STRING_AGG(c.comment, E'\\n' ORDER BY c.created_at) AS notes "
        "FROM support_tickets t "
        "LEFT JOIN support_ticket_comments c ON c.ticket_id = t.id AND c.tenant = t.tenant "
        "WHERE t.tenant = %s AND t.id = %s "
        "GROUP BY t.id"
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


async def add_comment(
    tenant: str, ticket_id: int, comment: str, tool_call_id: str | None = None
) -> bool:
    """Inserts one new comment row for `ticket_id`, scoped to `tenant`.
    Returns False if no ticket with that id exists for this tenant —
    checked explicitly, rather than relying on the FK, so a bad id never
    even attempts the insert.

    `tool_call_id` (postgres-init/15-append-notes-as-rows.sql) makes a
    genuine replay under the same id a no-op (`ON CONFLICT DO NOTHING`)
    instead of a second, duplicated comment — the same exactly-once-at-
    the-row shape `create_ticket`'s own docstring explains, finally
    reachable here now that each comment is its own row instead of text
    appended onto a shared column. `None` (the default) behaves exactly
    as before.

    The insert and the `updated_at` bump below are both inside this ONE
    `async with get_connection()` block on purpose — that block IS a
    transaction (see get_connection's own docstring), so the two commit
    or roll back together with no explicit `BEGIN`/`COMMIT` needed."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM support_tickets WHERE tenant = %s AND id = %s", [tenant, ticket_id]
        )
        if await cur.fetchone() is None:
            return False
        await conn.execute(
            "INSERT INTO support_ticket_comments (tenant, ticket_id, comment, tool_call_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tool_call_id) DO NOTHING",
            [tenant, ticket_id, comment, tool_call_id],
        )
        # Preserves the old column-append version's side effect (a new
        # comment bumps the ticket's own updated_at) — harmless to redo
        # unconditionally even on a no-op conflict above.
        await conn.execute(
            "UPDATE support_tickets SET updated_at = now() WHERE tenant = %s AND id = %s",
            [tenant, ticket_id],
        )
        return True
