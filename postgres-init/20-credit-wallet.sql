-- The credit wallet (app/billing/credits.py): what a tenant may still consume, as an append-only
-- ledger of grants, debits and expiries. specs/010-credit-billing-readiness, data-model.md.
--
-- Why the app owns this and the payment provider does not: Stripe applies credit grants to an
-- invoice only when it finalizes, and Polar "doesn't block usage if the customer exceeds their
-- balance". Neither can answer "may this request run NOW?", so the real-time balance has to live
-- here. The provider owns the MONEY; this owns the ENTITLEMENT.
--
-- Shape:
--   credit_accounts      a tenant is on credit billing if and only if it has a row here. A tenant
--                        with no account is never debited and never gated, so adding this table
--                        changes nothing for an existing tenant until an operator (or a verified
--                        purchase webhook) opens one.
--   credit_lots          one row per grant, with a `remaining` cache so a debit is O(lots), not
--                        O(every debit ever made). Debits consume the earliest-expiring lot first.
--                        A tenant has at most one 'overdraft' lot, which holds what was consumed
--                        beyond the balance as a negative `remaining` and is repaid first by the
--                        next grant.
--   credit_transactions  the idempotency unit. UNIQUE (tenant, idempotency_key): replaying a grant,
--                        debit or expiry with the same key inserts nothing.
--   credit_entries       the signed movements. THE TRUTH: a lot's `remaining` must always equal
--                        the sum of its entries (credits.verify() reports any drift).
--
-- Invariants enforced HERE, because application code must not be the only guard:
--   * transactions and entries are append-only: UPDATE and DELETE are refused;
--   * a lot never changes except its `remaining`; it is never deleted;
--   * an entry's lot and transaction belong to the SAME tenant (composite foreign keys), so a bug
--     cannot move credits across tenants (constitution I: a child table carries its own tenant);
--   * no lot goes negative except the overdraft lot.
--
-- `usage_event_id` on a transaction is a plain reference, NOT a foreign key: usage events are
-- trimmed by a retention job, and the wallet is a financial record that must outlive them.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing one, apply by
-- hand once:
--   psql -U langfuse -d appdata -f postgres-init/20-credit-wallet.sql
-- Until then nothing breaks: no code path reads these tables for a tenant with no account, and
-- no tenant has one.
\connect appdata

CREATE TABLE IF NOT EXISTS credit_accounts (
    tenant     TEXT PRIMARY KEY CHECK (tenant <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_lots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant       TEXT NOT NULL REFERENCES credit_accounts (tenant),
    source       TEXT NOT NULL
        CHECK (source IN ('purchase', 'subscription', 'promo', 'manual', 'adjustment', 'overdraft')),
    provider     TEXT,
    external_ref TEXT,
    granted      NUMERIC(18, 6) NOT NULL CHECK (granted >= 0),
    remaining    NUMERIC(18, 6) NOT NULL,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   TEXT NOT NULL,
    UNIQUE (id, tenant),
    CHECK (source = 'overdraft' OR remaining >= 0),
    CHECK (remaining <= granted),
    CHECK (source <> 'overdraft' OR (granted = 0 AND expires_at IS NULL))
);

-- At most one overdraft lot per tenant, so "the debt" is a single row.
CREATE UNIQUE INDEX IF NOT EXISTS credit_lots_one_overdraft_idx ON credit_lots (tenant) WHERE source = 'overdraft';
-- The debit path reads a tenant's live lots in consumption order.
CREATE INDEX IF NOT EXISTS credit_lots_live_idx ON credit_lots (tenant, expires_at NULLS LAST, created_at) WHERE remaining > 0;

CREATE TABLE IF NOT EXISTS credit_transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant          TEXT NOT NULL REFERENCES credit_accounts (tenant),
    kind            TEXT NOT NULL CHECK (kind IN ('grant', 'debit', 'expire', 'adjust', 'clawback')),
    idempotency_key TEXT NOT NULL CHECK (idempotency_key <> ''),
    usage_event_id  TEXT,
    -- What a debit was priced from: the provider's cost (12 decimal places: a cheap call costs a
    -- fraction of a millionth of a dollar) and the rate and markup in force at the
    -- time, so changing either later never rewrites what a past call was charged. NULL for
    -- anything that is not a priced debit (a grant, an expiry, a manual correction).
    cost_usd        NUMERIC(18, 12),
    credits_per_usd NUMERIC(18, 6),
    markup          NUMERIC(18, 6),
    reason          TEXT,
    actor           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant, idempotency_key),
    UNIQUE (id, tenant)
);

CREATE TABLE IF NOT EXISTS credit_entries (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id UUID NOT NULL,
    tenant         TEXT NOT NULL,
    lot_id         UUID NOT NULL,
    amount         NUMERIC(18, 6) NOT NULL CHECK (amount <> 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (transaction_id, tenant) REFERENCES credit_transactions (id, tenant),
    FOREIGN KEY (lot_id, tenant) REFERENCES credit_lots (id, tenant)
);

CREATE INDEX IF NOT EXISTS credit_entries_tenant_idx ON credit_entries (tenant, id);
CREATE INDEX IF NOT EXISTS credit_entries_lot_idx ON credit_entries (lot_id);

CREATE OR REPLACE FUNCTION credit_refuse_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: correct a mistake with a new transaction, never an UPDATE or DELETE', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER credit_transactions_append_only BEFORE UPDATE OR DELETE ON credit_transactions
    FOR EACH ROW EXECUTE FUNCTION credit_refuse_change();
CREATE OR REPLACE TRIGGER credit_entries_append_only BEFORE UPDATE OR DELETE ON credit_entries
    FOR EACH ROW EXECUTE FUNCTION credit_refuse_change();

-- A lot is a grant. Its `remaining` is the one thing that moves; everything that says what the grant
-- WAS (who, how much, when it expires) is fixed for good.
CREATE OR REPLACE FUNCTION credit_lots_only_remaining_changes() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'credit_lots is append-only: a lot is never deleted';
    END IF;
    IF (NEW.id, NEW.tenant, NEW.source, NEW.provider, NEW.external_ref, NEW.granted, NEW.expires_at, NEW.created_at, NEW.created_by)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant, OLD.source, OLD.provider, OLD.external_ref, OLD.granted, OLD.expires_at, OLD.created_at, OLD.created_by) THEN
        RAISE EXCEPTION 'credit_lots: only "remaining" may change; a lot''s grant is fixed';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER credit_lots_guard BEFORE UPDATE OR DELETE ON credit_lots
    FOR EACH ROW EXECUTE FUNCTION credit_lots_only_remaining_changes();
