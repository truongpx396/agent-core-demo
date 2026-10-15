# Quickstart: validating the migration tool and the idempotency conflict

Runnable checks for a reviewer. They need Docker (a throwaway Postgres) and the repository's virtualenv. Nothing here touches your own database, and nothing needs a provider credential.

```bash
source .venv/bin/activate
export PYTHONPATH=$PWD/packages/billing/src
docker run -d --rm --name mig-qs -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
export APPDATA_DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres OTEL_EXPORTER_OTLP_ENDPOINT=
```

## PR A — the migration tool

1. **Empty database → applied.**
   `python -m credit_billing.cli.migrate --status` lists both scripts `pending` and the "no migration record yet" footer; it creates nothing (`\dt` shows no tables).
   `python -m credit_billing.cli.migrate` prints `applied` for both and `2 applied, 0 already current.`; exit 0.
2. **Second run → nothing.** Run it again: `current` for both, `0 applied, 2 already current.`; `SELECT script, applied_at FROM billing_schema_migrations` shows the same times as after step 1.
3. **The deploy gate.** `python -m credit_billing.cli.migrate --check; echo $?` prints `0`. Then `UPDATE billing_schema_migrations SET checksum = 'x' WHERE script = '22-billing.sql'`; `--check` now lists it `changed` and exits `1`; a plain run re-applies and restores it; `--check` exits `0`.
4. **Adoption of a host-initialised database.** `DROP TABLE billing_schema_migrations`, insert a wallet row (`python -m credit_billing.cli.credits grant --tenant acme --amount 5 --by me --reason qs`), run the tool: both scripts recorded, `show --tenant acme` still reports 5 credits.
5. **Two at once.** Drop the record table, then `python -m credit_billing.cli.migrate & python -m credit_billing.cli.migrate & wait`: both exit 0; `SELECT count(*) FROM billing_schema_migrations` is `2`.
6. **A busy table.** In `psql`: `BEGIN; LOCK TABLE credit_transactions IN ACCESS EXCLUSIVE MODE;` and leave it open. Force a re-apply (`UPDATE … SET checksum = 'x'`), run `python -m credit_billing.cli.migrate --lock-timeout-seconds 2`: it stops within about 2 s, exit `4`, `error: 20-credit-wallet.sql did not complete: could not get a lock within 2s …`. `ROLLBACK;` in `psql` and run again: exit 0.
7. **Unreachable database.** `APPDATA_DATABASE_URL=postgresql://u:SECRET@127.0.0.1:1/x python -m credit_billing.cli.migrate; echo $?`: within about ten seconds, one `error:` line, status `3`, the word `SECRET` nowhere in the output.

The same scenarios as automated tests: `cd packages/billing && pytest tests/test_migrate.py tests/test_migrate_cli.py -q` (hermetic) and `pytest tests/integration/test_migrate_real_postgres.py -m integration -q` (Docker).

## PR B — the idempotency conflict

With the schema applied (step 1 above):

1. **A grant, then the same grant.**
   `python -m credit_billing.cli.credits grant --tenant acme --amount 500 --by me --reason "pilot" --key K1` → `Grant applied`.
   Repeat the identical command → `Already applied under key K1 …; nothing changed.`
2. **A different request under the same key.** `… --amount 600 --key K1` → exit `2`, `error: idempotency key 'K1' was already used for a different request (amount: '500.000000' -> '600.000000') …`. `show --tenant acme` still says 500.
3. **A different reason.** `… --amount 500 --reason "other" --key K1` → exit `2`, naming `reason`.
4. **The relative-expiry trap and its fix.** `… --source promo --expires-in-days 30 --key K2` → applied. Repeat it a minute later → exit `2`, naming `expires_at` with both instants and `--expires-at <first instant>` as the way to retry. Run it with that `--expires-at` → `Already applied`. `--expires-at … --expires-in-days 30` together → refused as invalid input before any database access.
5. **Legacy rows.** `INSERT INTO credit_transactions (tenant, kind, idempotency_key, reason, actor) VALUES ('acme', 'grant', 'LEGACY', 'r', 'a');` (an INSERT is allowed on the append-only table, and a row written by the old code has no `request`), then `… --amount 600 --key LEGACY` → `Already applied …` (today's behaviour for a row with no recorded request).
6. **An existing database gains the column through the tool.** Create a database from the script as it was before the change (`packages/billing/tests/fixtures/20-credit-wallet.before-request-column.sql`), insert a wallet row, run the tool: `\d credit_transactions` shows `request jsonb`, the old row is untouched, replaying its key is a duplicate.

Automated: `pytest tests/test_idempotency_request.py tests/test_credits_cli.py -q` and `pytest tests/integration/test_idempotency_real_postgres.py -m integration -q`; the two webhook callers (a refund described by two events; a grant replayed with a later expiry) are in the integration file and in the repository's existing webhook suites, which must stay green unchanged.

```bash
docker stop mig-qs
```
