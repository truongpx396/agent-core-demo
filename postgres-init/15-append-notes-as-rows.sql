-- Closes the one duplicate-write gap postgres-init/14-tool-call-id-columns.sql
-- deliberately left open: an *appended* note (support_tickets.notes,
-- crm_leads.notes) has no row of its own to put `ON CONFLICT` on — two
-- different appends simply produce two different concatenated strings,
-- so a tool_call_id-replay (the SAME accepted race 14's own comment
-- describes) silently doubled the text instead of creating a detectable
-- duplicate row. The fix: each append becomes its own row, keyed by
-- tool_call_id exactly like 14's pure-INSERT tables, with the flattened
-- text computed at READ time (STRING_AGG) instead of stored as a blob —
-- app/domains/support/store.py::add_comment and
-- app/domains/sales/store.py::append_lead_note/mark_lead_lost/
-- find_or_create_lead all write through these instead of a text
-- concatenation now; app/domains/*/tools.py needed no changes at all,
-- since get_ticket/get_lead/lead_history still return a single `notes`
-- string under the same key, just computed differently.
--
-- `notes` dropped from both parent tables: nothing reads or writes it
-- once the above lands, and a demo's postgres-init scripts only ever run
-- against a fresh, empty database (Postgres's own docker-entrypoint
-- behavior) — there is no live data this needs to carry forward.
--
-- `tenant` is denormalized onto both child tables (not just inherited via
-- the FK) — same "a child table scopes itself too, never relies solely on
-- a join to its parent" discipline postgres-init/08-crm.sql's own
-- crm_followups already established.
\connect appdata

ALTER TABLE support_tickets DROP COLUMN notes;
ALTER TABLE crm_leads DROP COLUMN notes;

CREATE TABLE support_ticket_comments (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES support_tickets (id),
    comment TEXT NOT NULL,
    tool_call_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX support_ticket_comments_ticket_id_idx ON support_ticket_comments (ticket_id);

CREATE TABLE crm_lead_notes (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    lead_id INTEGER NOT NULL REFERENCES crm_leads (id),
    note TEXT NOT NULL,
    tool_call_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX crm_lead_notes_lead_id_idx ON crm_lead_notes (lead_id);
