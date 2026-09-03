-- Ops bot's own incident log (app/domains/ops/store.py) — lets an
-- investigation that finds a real anomaly record it durably instead of
-- only ever posting a one-off team-channel message that scrolls away.
--
-- Deliberately NOT `tenant`-scoped, unlike support_tickets/crm_leads: this
-- app's own operational metrics have no per-tenant dimension either (see
-- app/domains/ops/tools.py's module docstring — ctx here proves "a
-- legitimate caller of this deployment," not a row-scoping filter), so an
-- incident about THOSE metrics has nothing to scope by tenant. `opened_by`
-- (the reporting principal) is what this table attributes rows to instead.
\connect appdata

CREATE TABLE ops_incidents (
    id SERIAL PRIMARY KEY,
    opened_by TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX ops_incidents_status_created_idx ON ops_incidents (status, created_at DESC);
