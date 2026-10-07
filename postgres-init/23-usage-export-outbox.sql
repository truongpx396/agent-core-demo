-- Money going OUT: the usage export outbox (app/billing/export.py, scripts/billing_export_worker.py;
-- specs/010-credit-billing-readiness, T023).
--
-- A tenant linked to a provider that bills on usage (Stripe meters, Polar events) must have each model call reach
-- that provider exactly once. A fire-and-forget call from the turn path cannot promise that: the provider may be down
-- for hours, and Stripe accepts a meter event only if its timestamp is within the past 35 days, so a retry that never
-- gives up would one day send events the provider silently discards. So the export is an OUTBOX: a row per
-- (provider, event), created in the SAME TRANSACTION as the usage event so the two cannot disagree about what exists,
-- drained by a worker with bounded retries and a hard age limit that fails loudly instead of dropping.
--
--   pending   waiting to be sent (first attempt, or backing off after a retryable failure)
--   sent      the provider accepted it, or reported it as a duplicate (which is success, as Polar does)
--   expired   older than BILLING_EXPORT_MAX_AGE_DAYS and still unsent: given up on BEFORE the provider's own window
--             closes, counted and alerted, never silently dropped
--   failed    a permanent provider refusal, the attempt budget spent, or no customer link to send it under: alerted
--
-- Invariants enforced HERE, because application code must not be the only guard:
--   * (provider, event_id) is the PRIMARY KEY: the same call is queued at most once per provider, so a replayed event
--     never doubles it, and the event id is also what the provider is given as its own idempotency key;
--   * the event the row points at is the SAME TENANT's (a trigger), so a bug cannot export one tenant's usage under
--     another's name (constitution I: a child table carries its own tenant);
--   * a finished row (`sent`, `expired`, `failed`) is TERMINAL and what a row IS (provider, event, tenant, when it was
--     queued) never changes: only its progress (status, attempts, next attempt, last error, sent time) moves.
--
-- The foreign key means an event with an outbox row cannot be deleted. That is deliberate and is the guard behind the
-- rule that a retention job must not delete a usage event that has not been exported (spec D7): nothing deletes
-- usage events yet, and when something does it has to clear finished outbox rows first, on purpose.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by hand once:
--   psql -U langfuse -d appdata -f postgres-init/23-usage-export-outbox.sql
-- Until then nothing breaks: no provider that exports usage is enabled by default, so nothing writes or reads this table.
\connect appdata

-- Why the tenant check is a TRIGGER and not a composite foreign key (the way the wallet does it), which would need a
-- `UNIQUE (event_id, tenant)` on `usage_events`: a second unique constraint on that table BREAKS the duplicate story it
-- was built on. `INSERT ... ON CONFLICT (event_id) DO NOTHING` arbitrates conflicts on the event_id index only; two writers of
-- the same event racing can instead collide on the OTHER unique index, which is not the arbiter, and Postgres raises
-- UniqueViolation instead of doing nothing. That surfaced as a spurious "usage event write failed" (and its page) whenever a
-- replay raced a retry: found by a real-Postgres test failing 3 runs in 40, not by reading. So `usage_events` keeps exactly one
-- unique constraint, its primary key (a test pins it), and this table points at it with a plain foreign key.

CREATE TABLE IF NOT EXISTS usage_export_outbox (
    provider         TEXT NOT NULL CHECK (provider <> ''),
    event_id         TEXT NOT NULL REFERENCES usage_events (event_id),
    tenant           TEXT NOT NULL CHECK (tenant <> ''),
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'expired', 'failed')),
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A reason code or an exception CLASS NAME, never an exception's text (a client error can carry a URL or a key).
    last_error_class TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at          TIMESTAMPTZ,
    PRIMARY KEY (provider, event_id),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);

-- The worker claims due pending rows oldest first; the partial index is the whole of that read.
CREATE INDEX IF NOT EXISTS usage_export_outbox_due_idx ON usage_export_outbox (next_attempt_at, created_at) WHERE status = 'pending';

CREATE OR REPLACE FUNCTION usage_export_outbox_guard() RETURNS trigger AS $$
BEGIN
    IF (NEW.provider, NEW.event_id, NEW.tenant, NEW.created_at) IS DISTINCT FROM (OLD.provider, OLD.event_id, OLD.tenant, OLD.created_at) THEN
        RAISE EXCEPTION 'usage_export_outbox: what a row IS (provider, event, tenant, when queued) never changes';
    END IF;
    IF OLD.status <> 'pending' AND (NEW.status, NEW.attempts, NEW.sent_at) IS DISTINCT FROM (OLD.status, OLD.attempts, OLD.sent_at) THEN
        RAISE EXCEPTION 'usage_export_outbox: a % row is finished and cannot change', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER usage_export_outbox_no_rewrite BEFORE UPDATE ON usage_export_outbox
    FOR EACH ROW EXECUTE FUNCTION usage_export_outbox_guard();

-- A row exports ITS event under ITS event's tenant: refuse anything else at the door (see the note above on why this is not
-- a composite foreign key).
CREATE OR REPLACE FUNCTION usage_export_outbox_tenant_guard() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM usage_events WHERE event_id = NEW.event_id AND tenant = NEW.tenant) THEN
        RAISE EXCEPTION 'usage_export_outbox: an event can only be exported under its own tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER usage_export_outbox_tenant_matches_event BEFORE INSERT ON usage_export_outbox
    FOR EACH ROW EXECUTE FUNCTION usage_export_outbox_tenant_guard();
