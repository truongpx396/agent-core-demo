-- Tier-1 support copilot's own system of record (app/domains/support/store.py)
-- — same "this app's own operational data, its own table in the appdata
-- database" lifecycle as employees/usage_ledger, not a new database.
\connect appdata

CREATE TABLE support_tickets (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    requester TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    escalation_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX support_tickets_tenant_idx ON support_tickets (tenant);
