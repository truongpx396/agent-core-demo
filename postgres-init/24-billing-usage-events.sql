-- The package's own record of BILLABLE USAGE: one row per unit of usage a host application hands it
-- (credit_billing/usage.py, specs/013-billing-owns-the-meter).
--
-- Why it exists: a host application meters its own usage (the agent app keeps `usage_events`, shaped for its model
-- calls and read by its spend caps). Billing needs a record it can rate, charge, export and reconcile WITHOUT knowing
-- anything about that host, so this table holds only what billing needs and nothing a host would have to invent: who
-- (tenant), which unit (event_id, the host's own id), what category (kind), what it cost the host (cost_usd), what it
-- is worth in credits at the rate in force (credits, with the rate and markup used), and two descriptive fields a
-- usage-billing provider is told (model, tokens). Two stores, one flow, no shared table: the host writes its event
-- and, in the same transaction, calls the package, which writes this row.
--
-- Invariants enforced HERE, because application code must not be the only guard:
--   * (tenant, event_id) is the PRIMARY KEY and the table's ONLY unique constraint, so `INSERT ... ON CONFLICT
--     (tenant, event_id) DO NOTHING` is the duplicate story. A second unique constraint would make that unreliable:
--     two writers racing on the same event can collide on the constraint that is NOT the arbiter, and Postgres
--     raises instead of doing nothing (found on the host's table by a real-Postgres test, specs/010 T023);
--   * the tenant is part of the key and is on every row (constitution I), so two tenants may use the same event id;
--   * rows are APPEND-ONLY: UPDATE is always refused, DELETE is refused unless the retention job says so for its own
--     transaction (`SET LOCAL billing_usage_events.allow_delete = 'on'`);
--   * `cost_usd` and `credits` are NULL when unknown. Unknown is not free: a 0 would be billed as free. cost_usd has
--     twelve decimal places on purpose: a call on a cheap model costs a fraction of a millionth of a dollar, and
--     rounding it to six would store a $0.0000004 call as zero, a systematic loss for billing, not noise;
--   * credits are only meaningful with the rate that produced them (a table CHECK), so the row reproduces the charge.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing database run
--   credit-billing-migrate        (or `make billing-migrate`; `--status` shows what is applied, `--check` gates a deploy)
-- BEFORE deploying the code that writes it. By hand, once:
--   psql -U langfuse -d appdata -f postgres-init/24-billing-usage-events.sql
-- Until it is applied every recording fails: the host's own meter is unaffected (a failed billing write never fails the
-- caller), each failure is counted as agent_cost_governance_degraded_total{path="billing_event_write"} and the
-- BillingEventWriteFailing alert says what to apply.
\connect appdata

CREATE TABLE IF NOT EXISTS billing_usage_events (
    tenant          TEXT NOT NULL CHECK (tenant <> ''),
    event_id        TEXT NOT NULL CHECK (event_id <> ''),
    kind            TEXT NOT NULL CHECK (kind <> ''),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cost_usd        NUMERIC(18, 12) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    credits         NUMERIC(18, 6) CHECK (credits IS NULL OR credits >= 0),
    credits_per_usd NUMERIC(18, 6) CHECK (credits_per_usd IS NULL OR credits_per_usd > 0),
    markup          NUMERIC(18, 6) CHECK (markup IS NULL OR markup > 0),
    model           TEXT,
    tokens          INTEGER CHECK (tokens IS NULL OR tokens >= 0),
    PRIMARY KEY (tenant, event_id),
    CHECK (credits IS NULL OR (credits_per_usd IS NOT NULL AND markup IS NOT NULL))
);

-- Every read is by time (retention, reconciliation) or by key (the outbox's foreign key, which the primary key serves).
CREATE INDEX IF NOT EXISTS billing_usage_events_occurred_idx ON billing_usage_events (occurred_at);

CREATE OR REPLACE FUNCTION billing_usage_events_refuse_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'billing_usage_events is append-only: correct a mistake with a new row, never an UPDATE';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_usage_events_refuse_delete() RETURNS trigger AS $$
BEGIN
    -- Only the retention job deletes, and it must say so inside its own transaction; a session setting is
    -- transaction-local with SET LOCAL / set_config(..., true), so it cannot be left switched on.
    IF current_setting('billing_usage_events.allow_delete', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'billing_usage_events is append-only: only the retention job may delete (SET LOCAL billing_usage_events.allow_delete = ''on'')';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER billing_usage_events_no_update BEFORE UPDATE ON billing_usage_events
    FOR EACH ROW EXECUTE FUNCTION billing_usage_events_refuse_update();
CREATE OR REPLACE TRIGGER billing_usage_events_no_delete BEFORE DELETE ON billing_usage_events
    FOR EACH ROW EXECUTE FUNCTION billing_usage_events_refuse_delete();
