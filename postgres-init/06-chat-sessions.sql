-- Session directory for the web UI's session switcher (app/sessions.py) —
-- same `appdata` database as employees/usage_ledger (this app's own
-- operational data lifecycle, GRAPH_PATTERNS.md pattern 26), a new table
-- rather than reusing usage_ledger (which no-ops on a zero-token/rejected
-- turn and has no title) or the checkpointer's own `checkpoints` table
-- (no tenant/principal column at all — not scoped to an owner).
\connect appdata

CREATE TABLE chat_sessions (
    thread_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX chat_sessions_tenant_principal_idx
    ON chat_sessions (tenant, principal, last_active_at DESC);
