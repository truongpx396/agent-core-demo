-- One row per MODEL CALL (app/agent/usage_events.py): the meter a credit or usage-billing product
-- is built on (specs/010-credit-billing-readiness).
--
-- Why it is not `usage_ledger`: that table is one row per completed TURN, with no key that
-- identifies a call, so a replayed write cannot be told from a new one and a provider that
-- deduplicates usage by a caller-supplied id (Stripe `identifier`, Polar `external_id`) has
-- nothing to be given. `event_id` is that key: deterministic from the call's own identity, so
-- the same call is the same row however many times it is written or exported.
--
-- Invariants this script enforces, because application code is not allowed to be the only guard:
--   * `event_id` is the PRIMARY KEY, so `INSERT ... ON CONFLICT DO NOTHING` is the duplicate story;
--   * rows are APPEND-ONLY: UPDATE is always refused, DELETE is refused unless the retention job
--     says so for its own transaction (`SET LOCAL usage_events.allow_delete = 'on'`);
--   * `cost_usd` is NULL when the model had no price. Unknown is not free, and a 0 here would be
--     billed as such. It has 12 decimal places on purpose: a call on a cheap model, or an embedding,
--     costs a fraction of a millionth of a dollar, and rounding it to six places would store a
--     $0.0000004 call as exactly zero (a systematic loss, not noise, for billing);
--   * `kind` is a closed set, so a typo cannot create a category nobody reads.
--
-- Credit columns (the credits charged and the rate used) are added by the PR that introduces the
-- wallet, with ALTER TABLE; none are needed to meter.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by
-- hand once:
--   psql -U langfuse -d appdata -f postgres-init/19-usage-events.sql
-- Until then every event write fails: the turn still succeeds (a failed write never fails a turn),
-- each failure is counted as agent_cost_governance_degraded_total{path="usage_event_table_missing"}
-- and the UsageEventTableMissing alert says what to apply.
\connect appdata

CREATE TABLE IF NOT EXISTS usage_events (
    event_id               TEXT PRIMARY KEY,
    tenant                 TEXT NOT NULL,
    principal              TEXT NOT NULL,
    thread_id              TEXT NOT NULL,
    kind                   TEXT NOT NULL
        CHECK (kind IN ('chat', 'followups', 'compaction', 'subagent', 'embedding', 'cron')),
    model_alias            TEXT NOT NULL,
    resolved_model         TEXT,
    input_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cached_input_tokens    INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    total_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_usd               NUMERIC(18, 12) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    price_input_per_token  NUMERIC(18, 12),
    price_output_per_token NUMERIC(18, 12),
    occurred_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The reads that matter are per tenant over a time window (reconciliation, export, a tenant's
-- usage), the same shape that earned usage_ledger its own (tenant, recorded_at) index.
CREATE INDEX IF NOT EXISTS usage_events_tenant_occurred_idx ON usage_events (tenant, occurred_at);

CREATE OR REPLACE FUNCTION usage_events_refuse_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'usage_events is append-only: correct a mistake with a new row, never an UPDATE';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION usage_events_refuse_delete() RETURNS trigger AS $$
BEGIN
    -- Only the retention job deletes, and it must say so inside its own transaction; a session
    -- setting is transaction-local with SET LOCAL, so it cannot be left switched on.
    IF current_setting('usage_events.allow_delete', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'usage_events is append-only: only the retention job may delete (SET LOCAL usage_events.allow_delete = ''on'')';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION usage_events_refuse_update();
CREATE OR REPLACE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION usage_events_refuse_delete();
