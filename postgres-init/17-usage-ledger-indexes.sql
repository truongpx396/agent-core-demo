-- Indexes for the rolling-window reads on usage_ledger (spec 008 A3).
--
-- 03-meter.sql gave the table one index, (tenant, principal). The two reads that
-- run on every turn filter by (tenant, recorded_at) — the tenant allowance — and
-- GET /usage does the same. Measured on a throwaway Postgres with 600,000 rows
-- (200,000 for one tenant over 90 days, 2,223 of them in the last 24 h): the
-- shipped index made the allowance read walk all 200,000 of the tenant's entries
-- (5,770 buffers, 9.3 ms); with (tenant, recorded_at) it read 1,857 buffers in
-- 1.5 ms. Small today, linear in history, and paid before every turn.
--
-- The second index serves the same read narrowed to one person
-- (tenant, principal, recorded_at) — a per-person allowance's window — and makes
-- the old (tenant, principal) index redundant, since it is a prefix of this one;
-- it is dropped so the ledger's insert path maintains one fewer index.
--
-- CONCURRENTLY: building an index normally blocks INSERTs for the duration, and
-- those inserts are every completed turn's ledger write (which fails open — a
-- blocked-then-failed write is a LOST row). CONCURRENTLY cannot run inside a
-- transaction, so apply this file with plain `psql -f`, not --single-transaction.
-- On a fresh volume the table is empty and this is instant.
--
-- Applying it: the init scripts only run on a fresh volume. Against an existing
-- one, apply by hand once:
--   psql -U langfuse -d appdata -f postgres-init/17-usage-ledger-indexes.sql
-- Until then nothing breaks; the reads are just the slower scan above. Every
-- statement is IF [NOT] EXISTS, so re-applying is harmless.
\connect appdata

CREATE INDEX CONCURRENTLY IF NOT EXISTS usage_ledger_tenant_recorded_at_idx
    ON usage_ledger (tenant, recorded_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS usage_ledger_tenant_principal_recorded_at_idx
    ON usage_ledger (tenant, principal, recorded_at);

DROP INDEX IF EXISTS usage_ledger_tenant_principal_idx;
