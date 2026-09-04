-- Domain-scoping the session switcher (app/agent/sessions.py,
-- GRAPH_PATTERNS.md pattern 49) — the queued chat endpoints
-- (app/api/main.py's X-Domain header) can now run a turn against any
-- registered domain (app/domains/registry.py), not just Acme, so a
-- session belongs to whichever domain's graph/tools/system-prompt it was
-- actually opened under. `'acme'` default backfills every row that
-- existed before this column did — true by construction, since every
-- domain besides Acme was added to this app after chat_sessions was.
\connect appdata

ALTER TABLE chat_sessions ADD COLUMN domain TEXT NOT NULL DEFAULT 'acme';

-- Extends the switcher's own composite index (06-chat-sessions.sql) with
-- domain, in the same column order list_sessions' WHERE clause filters by.
DROP INDEX chat_sessions_tenant_principal_idx;
CREATE INDEX chat_sessions_tenant_principal_domain_idx
    ON chat_sessions (tenant, principal, domain, last_active_at DESC);
