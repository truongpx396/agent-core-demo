-- Real usage/cost ledger (app/meter.py) — the standalone-product default
-- Meter, in the same `appdata` database app/sql_store.py already uses
-- (GRAPH_PATTERNS.md pattern 26). A table, not a new database: this is
-- the same "this app's own operational data" lifecycle as `employees`.
\connect appdata

CREATE TABLE usage_ledger (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    model_alias TEXT NOT NULL,
    total_tokens INTEGER NOT NULL,
    cost_usd NUMERIC(12, 6) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX usage_ledger_tenant_principal_idx ON usage_ledger (tenant, principal);
