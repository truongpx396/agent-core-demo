-- Money coming IN: the tenant link, the product catalog, and the webhook inbox
-- (app/billing/webhooks.py, app/billing/store.py; specs/010-credit-billing-readiness, T019).
--
-- A payment provider's webhook proves that a payment happened. It must never decide WHO is paid or HOW
-- MUCH, because a webhook endpoint cannot carry the app's tenant identity and anyone who finds it can
-- post to it. So both answers come from tables the app wrote itself:
--
--   billing_customers        tenant <-> the provider's customer id, written by the app when it creates the
--                            provider-side customer at checkout, NEVER from a webhook. It is the only way a
--                            webhook reaches a tenant. An unlinked customer is quarantined, never granted
--                            to a default tenant (constitution I: unknown resolves to refusal).
--   credit_products          the server-side catalog: what a product is worth in credits (and when it
--                            expires). A purchase's value is read from here, never from the payload amount.
--   billing_webhook_events   the inbox: one row per provider event, deduplicated by (provider, event_id).
--
-- Invariants enforced HERE, because application code must not be the only guard:
--   * (provider, event_id) is the PRIMARY KEY: delivering the same event twice, or two copies at once, is
--     one row, and the second delivery blocks on the first's transaction rather than racing it;
--   * a customer id belongs to ONE tenant (UNIQUE (provider, customer_ref)) and a tenant has ONE customer
--     per provider (UNIQUE (tenant, provider)), so a link cannot be silently re-pointed at another tenant;
--   * an `applied` or `ignored` event is TERMINAL: a trigger refuses to move it, so no bug can apply a
--     payment twice by flipping its status back. (`quarantined` may be re-opened by an operator, e.g. once
--     the missing customer link exists.) What an event WAS (provider, id, payload, when received) never
--     changes after it is written;
--   * `payload` holds only the NORMALIZED, whitelisted fields of an event (app/billing/providers/base.py,
--     BillingEvent.stored), never the provider's raw body, so a buyer's name, email or card details are
--     never stored here (constitution VI).
--
-- Why a retention sweep may delete `applied`/`ignored` rows without risking a double grant: the grant's own
-- idempotency key, "{provider}:{event_id}" in credit_transactions (20), is a financial record kept for good,
-- so an event redelivered after its inbox row was swept inserts a fresh row and then finds the grant already made.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by hand once:
--   psql -U langfuse -d appdata -f postgres-init/22-billing.sql
-- Until then nothing breaks: no provider is configured by default (BILLING_PROVIDERS), so nothing reads these tables.
\connect appdata

CREATE TABLE IF NOT EXISTS billing_customers (
    tenant       TEXT NOT NULL CHECK (tenant <> ''),
    provider     TEXT NOT NULL CHECK (provider <> ''),
    customer_ref TEXT NOT NULL CHECK (customer_ref <> ''),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, customer_ref),
    UNIQUE (tenant, provider)
);

CREATE TABLE IF NOT EXISTS credit_products (
    provider           TEXT NOT NULL CHECK (provider <> ''),
    product_ref        TEXT NOT NULL CHECK (product_ref <> ''),
    credits            NUMERIC(18, 6) NOT NULL CHECK (credits > 0),
    -- NULL means the credits do not expire. The wallet itself refuses a subscription grant with no expiry
    -- (credits.EXPIRY_REQUIRED), so a subscription product without one is quarantined, not mis-granted.
    expires_after_days INTEGER CHECK (expires_after_days IS NULL OR expires_after_days > 0),
    active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, product_ref)
);

CREATE TABLE IF NOT EXISTS billing_webhook_events (
    provider    TEXT NOT NULL CHECK (provider <> ''),
    event_id    TEXT NOT NULL CHECK (event_id <> ''),
    event_type  TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL CHECK (status IN ('received', 'applied', 'ignored', 'quarantined', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    -- NULL until the customer link resolves it. Always the LINKED tenant, never a field from the payload.
    tenant      TEXT,
    payload     JSONB NOT NULL,
    -- A reason code, never an exception message (a driver error can carry a host, SQL or a DSN).
    error_class TEXT,
    PRIMARY KEY (provider, event_id)
);

-- One grant per PAYMENT. A provider can describe one payment in several events (Stripe sends more than one
-- for a checkout), each with its own event id, so the inbox key and the grant's "{provider}:{event_id}" key
-- would both let it through twice. This is the backstop that holds even when two such events race: the second
-- grant fails here, loudly, instead of crediting the customer twice. (The app checks first and ignores the
-- second event gracefully; this index is what stops a race between the check and the insert.)
CREATE UNIQUE INDEX IF NOT EXISTS credit_lots_one_grant_per_payment_idx
    ON credit_lots (tenant, provider, external_ref)
    WHERE source IN ('purchase', 'subscription') AND external_ref IS NOT NULL;

-- The sweep and any "what is stuck?" question read by status and age.
CREATE INDEX IF NOT EXISTS billing_webhook_events_status_idx ON billing_webhook_events (status, received_at);

CREATE OR REPLACE FUNCTION billing_webhook_events_guard() RETURNS trigger AS $$
BEGIN
    IF (NEW.provider, NEW.event_id, NEW.event_type, NEW.received_at, NEW.payload)
       IS DISTINCT FROM (OLD.provider, OLD.event_id, OLD.event_type, OLD.received_at, OLD.payload) THEN
        RAISE EXCEPTION 'billing_webhook_events: what an event WAS (provider, id, type, payload, when received) never changes';
    END IF;
    IF OLD.status IN ('applied', 'ignored') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'billing_webhook_events: an % event is terminal and cannot become %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER billing_webhook_events_no_rewrite BEFORE UPDATE ON billing_webhook_events
    FOR EACH ROW EXECUTE FUNCTION billing_webhook_events_guard();
