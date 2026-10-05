-- Per-tenant and per-person OVERRIDES of the spend limits (app/agent/budget_policies.py).
--
-- The limits themselves come from Settings (MAX_COST_USD_PER_TENANT_PER_DAY, ...). Those
-- apply to everyone identically, which is wrong the moment tenants are on different plans or
-- one account has to be stopped: this table lets an operator change one tenant's, or one
-- person's, limit without redeploying anyone.
--
-- `subject` says whose limit a row is:
--     ''         the tenant's own limit
--     '*'        the personal limit of EVERY person in the tenant (a per-tenant plan tier)
--     <id>       one person's limit (beats '*', which beats the Settings default)
-- `period` is 'day' (rolling 24h) or 'month' (calendar month, UTC).
-- `limit_usd`:
--     NULL       explicitly NO cap, overriding a Settings default (a trusted internal tenant)
--     0          a cap of zero — every turn is refused. This is the "suspend" switch.
--     N          a cap of N dollars
-- A row replaces the default for its (subject, period); there is no row to say "use the
-- default" — delete the row.
--
-- Written only by an operator (scripts/budget_policy.py), never by a request, and read before
-- every turn through a short in-process cache (BUDGET_POLICY_REFRESH_SECONDS), so a change
-- takes up to that long to reach a running worker.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by
-- hand once:
--   psql -U langfuse -d appdata -f postgres-init/18-budget-policies.sql
-- Until then overrides are simply ignored (the read finds no table, logs once and uses the
-- Settings defaults), so a SUSPEND set before this is applied is not enforced.
\connect appdata

CREATE TABLE IF NOT EXISTS budget_policies (
    tenant     TEXT NOT NULL,
    subject    TEXT NOT NULL,
    period     TEXT NOT NULL CHECK (period IN ('day', 'month')),
    limit_usd  NUMERIC(12, 6) CHECK (limit_usd IS NULL OR limit_usd >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL,
    PRIMARY KEY (tenant, subject, period)
);
