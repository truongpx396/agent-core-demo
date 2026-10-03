-- One row per in-flight turn, replacing 12-tenant-budget-reservations.sql's one
-- row per tenant.
--
-- The old design kept a single running total per tenant plus one `updated_at`,
-- and `updated_at` had to mean two things at once: "when was this total last
-- touched" (every reserve and every release bumps it) and "how old is the
-- amount this total still holds". Two defects fell out of that, both
-- reproduced against a real Postgres (spec 008, B20):
--   * a worker killed mid-turn never released its amount. After five minutes
--     the READ ignored the row, but the next reserve ADDED to the stale
--     amount and refreshed the timestamp, so the dead turn's amount came
--     back as if it were in flight and was never released again;
--   * while a tenant kept running turns, each reserve/release refreshed
--     `updated_at`, so a leaked amount never aged out at all — 40 simulated
--     minutes after the leak it still counted.
-- Either one can push a tenant over MAX_COST_USD_PER_TENANT_PER_DAY on spend
-- that never happened, which refuses its real turns with no signal.
--
-- Here each turn's hold carries its own `created_at`, so an abandoned hold ages
-- out on its own clock whatever else the tenant does: the read sums only holds
-- younger than RESERVATION_STALE_AFTER_MINUTES, a release deletes exactly its
-- own hold (a second release is a no-op, not a second subtraction), and
-- reserve sweeps that tenant's abandoned holds so the table stays small.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing
-- one, apply this file by hand once (`psql -U langfuse -d appdata -f
-- postgres-init/16-tenant-budget-holds.sql`). Until then reserve_budget fails
-- open — it logs `tenant_budget_reservation_failed` and the turn runs without
-- the concurrent-turn protection, exactly the behaviour before reservations
-- existed.
--
-- `tenant_budget_reservations` (12-*.sql) is left in place and unused so a
-- rolling deploy never has an old worker writing to a table that is gone; drop
-- it in a later release once no old worker is running.
\connect appdata

CREATE TABLE tenant_budget_holds (
    hold_id UUID PRIMARY KEY,
    tenant TEXT NOT NULL,
    reserved_usd NUMERIC(12, 6) NOT NULL CHECK (reserved_usd > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The read (SUM over one tenant's fresh holds) and the sweep (DELETE one
-- tenant's old holds) both filter on exactly these two columns.
CREATE INDEX tenant_budget_holds_tenant_created_at
    ON tenant_budget_holds (tenant, created_at);
