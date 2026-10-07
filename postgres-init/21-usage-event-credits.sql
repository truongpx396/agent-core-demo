-- What a model call was WORTH in credits, stored on its usage event
-- (app/agent/usage_events.py, specs/010-credit-billing-readiness, task T015).
--
-- `usage_events` (19) records what a call cost in dollars. Once credits exist a call also has a
-- price in them: `credits = round_half_up(cost_usd x credits_per_usd x markup, 6)`. All three
-- figures are stored on the row, so that
--   * the credits are reproducible from the row alone (reconciliation recomputes them);
--   * changing CREDITS_PER_USD or MARKUP later never rewrites what a past call was charged
--     (the spec's edge case: "past events keep the credits they were charged").
--
-- `credits` is what the call is WORTH at the rate in force, whether or not the tenant has a wallet
-- to charge: it is the same number a usage-billing provider is sent. Whether a wallet was
-- actually debited is the `credit_transactions` row keyed by this event's id, not this column.
--
-- NULL means no credit price: credits are off (no CREDITS_PER_USD) or the call was UNPRICED. As
-- with `cost_usd`, NULL is "unknown", never 0, because a 0 would be billed as free. A genuinely
-- free call (a local model) is a real 0.
--
-- Nothing here needs `usage_events` to be rewritten: ADD COLUMN touches no row, and the table's
-- append-only triggers (UPDATE and DELETE) are not involved.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by
-- hand once, BEFORE setting CREDITS_PER_USD (until it is set, no code reads or writes these
-- columns and the app behaves exactly as it did without them):
--   psql -U langfuse -d appdata -f postgres-init/21-usage-event-credits.sql
-- If CREDITS_PER_USD is set first, every event write fails and is counted as
-- agent_cost_governance_degraded_total{path="usage_event_table_missing"} (alert UsageEventTableMissing).
\connect appdata

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS credits         NUMERIC(18, 6) CHECK (credits IS NULL OR credits >= 0),
    ADD COLUMN IF NOT EXISTS credits_per_usd NUMERIC(18, 6) CHECK (credits_per_usd IS NULL OR credits_per_usd > 0),
    ADD COLUMN IF NOT EXISTS markup          NUMERIC(18, 6) CHECK (markup IS NULL OR markup > 0);

-- Credits are only meaningful with the rate that produced them. (ADD CONSTRAINT has no
-- IF NOT EXISTS, so the guard is spelled out to keep this script re-runnable.)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'usage_events_credits_have_a_rate' AND conrelid = 'usage_events'::regclass
    ) THEN
        ALTER TABLE usage_events ADD CONSTRAINT usage_events_credits_have_a_rate
            CHECK (credits IS NULL OR (credits_per_usd IS NOT NULL AND markup IS NOT NULL));
    END IF;
END
$$;
