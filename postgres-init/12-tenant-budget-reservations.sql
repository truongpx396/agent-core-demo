-- Closes a check-then-act race in app/agent/runtime.py's
-- _tenant_over_daily_budget: it sums usage_ledger BEFORE a turn runs, and
-- usage_ledger.record_usage only writes that turn's own cost AFTER it
-- completes — nothing in between accounts for turns that are already
-- running but haven't landed a ledger row yet. N concurrent turns from
-- the same tenant could all read the same "spent so far", all pass the
-- check, and all proceed, overshooting MAX_COST_USD_PER_TENANT_PER_DAY by
-- up to N turns' worth under real concurrent load.
--
-- One row per tenant holding its current IN-FLIGHT reservation:
-- app/agent/usage_ledger.py's reserve_budget/release_budget_reservation
-- atomically add/subtract MAX_COST_USD_PER_TURN around each turn (UPSERT,
-- so no separate seed row is needed), and _tenant_over_daily_budget adds
-- this to usage_ledger's own persisted sum when checking the cap.
-- `updated_at` is read back in in_flight_reservation to ignore a stale
-- reservation (a worker that died mid-turn without releasing) rather than
-- letting it permanently inflate a tenant's apparent spend — the same
-- self-healing-via-staleness idea app/job_queue/queue.py's THREAD_LOCK_TTL_SECONDS
-- uses for a crashed worker's lock, applied here without Redis's native
-- TTL since this lives in Postgres alongside the ledger it complements.
\connect appdata

CREATE TABLE tenant_budget_reservations (
    tenant TEXT PRIMARY KEY,
    reserved_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
