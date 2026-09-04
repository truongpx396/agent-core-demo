"""Session directory for the web UI's session switcher — lets a user list
and switch between their own past conversation threads.

Persisted in the same `appdata` Postgres database app/agent/sql_store.py and
app/agent/meter.py already use (`chat_sessions` table, postgres-init/06-chat-sessions.sql)
— this app's own operational-data lifecycle, not a new database. A
dedicated table rather than reusing `usage_ledger`: that table no-ops on
a zero-token/rejected turn (app/agent/meter.py::record_usage) and carries no
title, so a session with no billable tokens (a moderation block, a cache
hit) would silently vanish from a "resume my conversations" list built
from it. The checkpointer's own `checkpoints` table has `thread_id` but
no tenant/principal column at all — not scoped to an owner, and
deserializing a title preview out of LangGraph's internal checkpoint blob
format from application code would be fragile across a
langgraph-checkpoint-postgres version bump.

Every row is tenant+principal scoped, the same isolation axis every other
write in this app already carries (app/core/security.py's SecurityCtx) —
and, since GRAPH_PATTERNS.md pattern 49, domain-scoped too: a session
belongs to whichever domain's graph/tools/system-prompt (app/domains/registry.py)
actually ran on it, not just to a tenant+principal, since a thread_id
resumed under a DIFFERENT domain than it was opened in would run that
domain's tools/prompt against a conversation history that was never built
around them (see app/turns/queue.py::publish_request's own docstring on
the identical hazard for a queued resume/cancel). `domain` defaults to
`"acme"` everywhere in this module — same "no domain given" convention
`app/turns/queue.py::requests_stream_key`/`app/core/config.py`'s
`AGENT_DOMAIN` already establish — and is set ONLY on first insert
(`upsert_session`'s `ON CONFLICT` clause never touches it, same as
`title`): a session's domain is fixed at whichever one actually created
it, immutable after, regardless of which domain is later selected.
"""
import logging

from app.agent.sql_store import get_connection
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60


def upsert_session(
    ctx: SecurityCtx | None, thread_id: str, title: str | None = None, domain: str = "acme"
) -> None:
    """Best-effort write-through at the start of every turn (app/agent/runtime.py's
    stream_turn/answer/astream_events_turn, right after seeding) — NOT
    gated on the turn actually completing or producing any tokens, unlike
    app/agent/meter.py::record_usage, specifically so a rejected/moderated/
    short-circuited turn still shows up in the session list (the user did
    start a real conversation on this thread_id, whatever happened next).

    `title`/`domain` are set ONLY on first insert (the ON CONFLICT clause
    below never touches either) — later turns on the same thread_id only
    refresh `last_active_at`, so the session's displayed name AND its
    domain stay whatever its opening turn actually was, never silently
    reassigned by a later turn run against a different one. A ledger write
    that fails must not fail the turn whose session it's trying to record
    — same degrade-don't-crash posture as record_usage.
    """
    if not valid_ctx(ctx) or not thread_id:
        return
    display_title = (title or "New conversation").strip() or "New conversation"
    if len(display_title) > TITLE_MAX_CHARS:
        display_title = display_title[:TITLE_MAX_CHARS].rstrip() + "…"
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                (thread_id, ctx["tenant"], ctx["principal"], display_title, domain),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat session upsert failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


def list_sessions(ctx: SecurityCtx | None, domain: str = "acme") -> list[dict]:
    """Every session belonging to ctx's tenant+principal AND `domain`, most
    recently active first. Never scoped to tenant alone — a session
    belongs to whoever started it, the same owner-level isolation
    app/core/security.py::Policy.lower already applies to memories (as opposed
    to documents, which are tenant-shared) — with domain now a THIRD
    scoping axis alongside it (see this module's own docstring): the same
    principal's "support" and "sales" conversations never appear in each
    other's switcher."""
    if not valid_ctx(ctx):
        return []
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT thread_id, title, created_at, last_active_at FROM chat_sessions "
            "WHERE tenant = %s AND principal = %s AND domain = %s ORDER BY last_active_at DESC",
            (ctx["tenant"], ctx["principal"], domain),
        )
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def session_belongs_to(ctx: SecurityCtx | None, thread_id: str, domain: str = "acme") -> bool:
    """Ownership check for GET /chat/sessions/{thread_id}/messages —
    app/agent/runtime.py::get_session_messages reads the shared Postgres
    checkpointer directly, which carries no tenant/principal (or domain) of
    its own to check against (see this module's docstring), so the CALLER
    must verify ownership here first, against this table, before ever
    reading that thread_id's transcript. A single targeted row lookup, not
    `thread_id in {s["thread_id"] for s in list_sessions(ctx, domain)}` —
    no reason to fetch every session just to check membership of one.
    Requiring `domain` to match too (not just tenant+principal) means a
    thread opened under a different domain reads as "not found" here,
    exactly like a different tenant's or principal's thread already did —
    it doesn't belong to THIS domain context, whoever owns it."""
    if not valid_ctx(ctx) or not thread_id:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
            "AND principal = %s AND domain = %s",
            (thread_id, ctx["tenant"], ctx["principal"], domain),
        )
        return cur.fetchone() is not None
