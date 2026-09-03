-- Running follow-up log on an existing ticket (app/domains/support/store.py
-- ::add_comment) — same "append, newest last" shape crm_leads.notes already
-- established (postgres-init/08-crm.sql), so a customer adding more detail
-- to an open ticket doesn't need a second table just to hold free text.
\connect appdata

ALTER TABLE support_tickets ADD COLUMN notes TEXT;
