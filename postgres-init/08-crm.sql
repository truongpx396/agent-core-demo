-- Sales/CRM concierge's own system of record (app/domains/sales/store.py)
-- — same "this app's own operational data" lifecycle as employees/
-- usage_ledger/support_tickets, a table in the existing appdata database.
\connect appdata

CREATE TABLE crm_leads (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One lead per (tenant, contact) — log_lead_interaction find-or-creates by
-- this pair rather than ever inserting a duplicate lead for the same
-- customer contact.
CREATE UNIQUE INDEX crm_leads_tenant_contact_idx ON crm_leads (tenant, contact);

CREATE TABLE crm_followups (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    lead_id INTEGER NOT NULL REFERENCES crm_leads (id),
    due_at TIMESTAMPTZ NOT NULL,
    note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- scripts/followup_sweep.py's own query shape: pending items due by now,
-- scoped to tenant.
CREATE INDEX crm_followups_tenant_due_idx ON crm_followups (tenant, due_at)
    WHERE status = 'pending';
