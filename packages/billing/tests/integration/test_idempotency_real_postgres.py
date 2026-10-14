"""A reused idempotency key, against a REAL Postgres (credit_billing/credits.py, spec 012-billing-migrate-idempotency).

The unique key `(tenant, idempotency_key)` makes a replay insert nothing; the stored request is what lets the wallet tell a replay from a
DIFFERENT request that reused the key. What the database does with it is proven here: that every field of every operation is noticed, that
nothing is written when a request is refused, that rows written before the column existed keep their old behaviour, that a database created
from the PRE-change script gains the column through the migration tool and keeps working, that two simultaneous first uses of one key
resolve to exactly one winner, and that a conflict never reaches across tenants.

Each test gets its own database (`tests/integration/databases.py`), so the wallet tables hold only that test's rows and the snapshots
below are exact.
"""
import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from credit_billing import credits, migrate, schema
from tests.containers import ensure_postgres
from tests.integration.databases import (
    Throwaway,
    create_database,
    init_like_the_host,
    open_connection,
)

pytestmark = pytest.mark.integration

D = Decimal
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "20-credit-wallet.before-request-column.sql"


@pytest.fixture(scope="module")
def container_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture
async def wallet(container_url, monkeypatch):
    """A database with the current schema, and the wallet module pointed at it."""
    database = await create_database(container_url)
    await init_like_the_host(database)
    monkeypatch.setattr(credits, "get_connection", database.connection)
    monkeypatch.setattr(migrate, "get_connection", database.connection)
    yield database
    await database.drop()


EXPIRES = datetime.now(UTC) + timedelta(days=10)
GRANT = {
    "amount": D("500"), "source": "promo", "provider": "stripe", "external_ref": "pi_1", "expires_at": EXPIRES, "reason": "r", "actor": "a",
}
PRICING = credits.Pricing(cost_usd=D("0.01"), credits_per_usd=D("1000"), markup=D("1"))
DEBIT = {
    "amount": D("10"), "usage_event_id": "u1", "pricing": PRICING, "reason": "model call", "actor": "system",
}
# the one change to make to the base request, per field: what a caller who reused the key with something different would send
GRANT_CHANGE = {
    "amount": {"amount": D("600")}, "source": {"source": "manual"}, "provider": {"provider": "polar"}, "external_ref": {"external_ref": "pi_2"},
    "expires_at": {"expires_at": EXPIRES + timedelta(seconds=1)}, "reason": {"reason": "other"}, "actor": {"actor": "b"},
}
DEBIT_CHANGE = {
    "amount": {"amount": D("11")}, "usage_event_id": {"usage_event_id": "u2"},
    "cost_usd": {"pricing": credits.Pricing(D("0.02"), D("1000"), D("1"))},
    "credits_per_usd": {"pricing": credits.Pricing(D("0.01"), D("2000"), D("1"))},
    "markup": {"pricing": credits.Pricing(D("0.01"), D("1000"), D("2"))},
    "reason": {"reason": "other"}, "actor": {"actor": "b"},
}


async def grant(tenant, key="k", **overrides):
    kwargs = {**GRANT, **overrides}
    async with credits.get_connection() as conn:
        return await credits.grant_in(
            conn, tenant, kwargs.pop("amount"), idempotency_key=key, **kwargs,
        )


async def debit(tenant, key="d", **overrides):
    kwargs = {**DEBIT, **overrides}
    async with credits.get_connection() as conn:
        return await credits.debit_in(conn, tenant, kwargs.pop("amount"), idempotency_key=key, **kwargs)


async def opened(wallet, tenant="acme"):
    """A tenant with a wallet and enough credit, so a debit has something to draw on."""
    await grant(tenant, key="seed", amount=D("1000"), source="manual", expires_at=None, provider=None, external_ref=None)


# --- every field of every operation is noticed ----------------------------------------------------------------------------------------------

def test_the_changes_below_cover_exactly_the_vocabulary():
    """If a field is added to the vocabulary, a case for it must be added here or this fails."""
    assert set(GRANT_CHANGE) == set(credits.GRANT_FIELDS) and set(DEBIT_CHANGE) == set(credits.DEBIT_FIELDS)


@pytest.mark.parametrize("field", credits.GRANT_FIELDS)
async def test_a_grant_key_reused_with_one_field_changed_is_refused_naming_that_field_and_nothing_is_written(wallet, field):
    first = await grant("acme")
    before = await wallet.snapshot()

    with pytest.raises(credits.IdempotencyConflict) as raised:
        await grant("acme", **GRANT_CHANGE[field])

    assert set(raised.value.differences) == {field}, "only the field that was changed"
    assert raised.value.key == "k" and raised.value.tenant == "acme"
    assert await wallet.snapshot() == before, "the refused request wrote nothing: no lot, no entry, no transaction"
    assert first.status == "applied"


@pytest.mark.parametrize("field", credits.DEBIT_FIELDS)
async def test_a_debit_key_reused_with_one_field_changed_is_refused_naming_that_field_and_nothing_is_written(wallet, field):
    await opened(wallet)
    await debit("acme")
    before = await wallet.snapshot()

    with pytest.raises(credits.IdempotencyConflict) as raised:
        await debit("acme", **DEBIT_CHANGE[field])

    assert set(raised.value.differences) == {field}
    assert await wallet.snapshot() == before
    assert await credits.verify("acme") == []


async def test_an_identical_retry_of_each_operation_is_a_duplicate_pointing_at_the_original_and_changes_nothing(wallet):
    first_grant = await grant("acme")
    await opened(wallet, "other")
    first_debit = await debit("other")
    before = await wallet.snapshot()

    again_grant = await grant("acme")
    again_debit = await debit("other")

    assert (again_grant.status, again_grant.transaction_id) == ("duplicate", first_grant.transaction_id)
    assert (again_debit.status, again_debit.transaction_id) == ("duplicate", first_debit.transaction_id)
    assert await wallet.snapshot() == before


async def test_a_clawback_and_an_adjustment_are_judged_like_a_debit_and_a_grant(wallet):
    await opened(wallet)
    async with credits.get_connection() as conn:
        await credits.clawback_in(conn, "acme", 5, idempotency_key="cb", actor="webhook:stripe", reason="payment_refunded")
        await credits.adjust_in(conn, "acme", D("-3"), idempotency_key="adj-", actor="operator:a", reason="fix")
        await credits.adjust_in(conn, "acme", D("4"), idempotency_key="adj+", actor="operator:a", reason="fix")
    before = await wallet.snapshot()

    async with credits.get_connection() as conn:
        again = await credits.clawback_in(conn, "acme", 5, idempotency_key="cb", actor="webhook:stripe", reason="payment_refunded")
        assert again.status == "duplicate"
        with pytest.raises(credits.IdempotencyConflict) as clawback:
            await credits.clawback_in(conn, "acme", 6, idempotency_key="cb", actor="webhook:stripe", reason="payment_refunded")
        with pytest.raises(credits.IdempotencyConflict) as negative:
            await credits.adjust_in(conn, "acme", D("-9"), idempotency_key="adj-", actor="operator:a", reason="fix")
        with pytest.raises(credits.IdempotencyConflict) as positive:
            await credits.adjust_in(conn, "acme", D("5"), idempotency_key="adj+", actor="operator:a", reason="fix")

    assert [set(e.value.differences) for e in (clawback, negative, positive)] == [{"amount"}] * 3
    assert await wallet.snapshot() == before


async def test_the_kind_is_always_compared_even_when_nothing_else_is(wallet):
    await opened(wallet)
    await grant("acme", key="K", amount=D("5"), source="manual", expires_at=None, provider=None, external_ref=None, reason="r", actor="a")

    with pytest.raises(credits.IdempotencyConflict) as raised:
        await debit("acme", key="K", amount=D("5"), reason="r", actor="a", usage_event_id=None, pricing=None, compare=frozenset())

    assert set(raised.value.differences) == {"kind"} and raised.value.differences["kind"] == ("grant", "debit")


async def test_a_narrowed_comparison_ignores_what_it_leaves_out_and_still_catches_what_it_keeps(wallet):
    await grant("acme")
    narrowed = frozenset(credits.GRANT_FIELDS) - {"expires_at"}

    later = await grant("acme", compare=narrowed, expires_at=EXPIRES + timedelta(hours=1))
    with pytest.raises(credits.IdempotencyConflict) as raised:
        await grant("acme", compare=narrowed, amount=D("600"), expires_at=EXPIRES + timedelta(hours=1))

    assert later.status == "duplicate", "the expiry is not part of what this caller means"
    assert set(raised.value.differences) == {"amount"}


async def test_an_unknown_field_is_refused_before_anything_is_written(wallet):
    before = await wallet.snapshot()

    with pytest.raises(ValueError, match="cannot compare"):
        await grant("acme", compare={"expiry"})

    assert await wallet.snapshot() == before


# --- what is stored ---------------------------------------------------------------------------------------------------------------------------

async def test_the_request_is_stored_as_json_in_the_documented_shape(wallet):
    await grant("acme")
    await opened(wallet, "other")
    await debit("other")

    grant_request = await wallet.scalar("SELECT request FROM credit_transactions WHERE tenant = 'acme' AND idempotency_key = 'k'")
    debit_request = await wallet.scalar("SELECT request FROM credit_transactions WHERE tenant = 'other' AND idempotency_key = 'd'")

    assert grant_request == {
        "kind": "grant", "amount": "500.000000", "source": "promo", "provider": "stripe", "external_ref": "pi_1",
        "expires_at": EXPIRES.astimezone(UTC).isoformat(timespec="microseconds"), "reason": "r", "actor": "a",
    }
    assert debit_request == {
        "kind": "debit", "amount": "10.000000", "usage_event_id": "u1", "cost_usd": "0.01", "credits_per_usd": "1000", "markup": "1",
        "reason": "model call", "actor": "system",
    }


async def test_an_expiry_records_the_lot_it_expired_and_a_second_sweep_does_nothing(wallet):
    await grant("acme", amount=D("10"), expires_at=datetime.now(UTC) + timedelta(seconds=1.5), source="promo")
    await asyncio.sleep(2)

    assert await credits.expire_due(limit=10, tenant="acme") == 1
    assert await credits.expire_due(limit=10, tenant="acme") == 0

    lot = await wallet.scalar("SELECT id FROM credit_lots WHERE tenant = 'acme' AND source = 'promo'")
    request = await wallet.scalar("SELECT request FROM credit_transactions WHERE tenant = 'acme' AND kind = 'expire'")
    assert request == {"kind": "expire", "lot_id": str(lot)}


# --- rows that predate the column ------------------------------------------------------------------------------------------------------------

async def test_a_transaction_with_no_stored_request_is_a_plain_duplicate_whatever_is_asked(wallet):
    """INSERTs are allowed on the append-only table; a row written by the code before this feature simply has no `request`."""
    async with wallet.connection() as conn:
        await conn.execute("INSERT INTO credit_accounts (tenant, created_by) VALUES ('acme', 'old')")
        await conn.execute(
            "INSERT INTO credit_transactions (tenant, kind, idempotency_key, reason, actor) VALUES ('acme', 'grant', 'legacy', 'r', 'a')"
        )
    before = await wallet.snapshot()

    answer = await grant("acme", key="legacy", amount=D("999"), source="manual", expires_at=None)

    assert answer.status == "duplicate" and await wallet.snapshot() == before, "no comparison, no error, nothing written"
    assert await wallet.scalar("SELECT request IS NULL FROM credit_transactions WHERE idempotency_key = 'legacy'") is True


async def test_reapplying_the_wallet_script_does_not_wait_behind_a_reader_of_the_hot_table(wallet):
    """Why the column is added behind a catalog check: a plain `ADD COLUMN IF NOT EXISTS` takes ACCESS EXCLUSIVE on the table even when
    the column is already there, so re-applying the script (which `migrate` does whenever its checksum changes) would wait behind any open
    read of the table every debit writes. With a 500 ms lock timeout the plain form fails here; the guarded one does not wait at all."""
    reader = await open_connection(wallet)
    try:
        await reader.execute("SELECT count(*) FROM credit_transactions")  # the read holds ACCESS SHARE until its transaction ends
        started = time.monotonic()
        async with wallet.connection() as conn:
            await conn.execute("SET LOCAL lock_timeout = '500ms'")
            await conn.execute(schema.executable("20-credit-wallet.sql"))

        assert time.monotonic() - started < 3
    finally:
        await reader.rollback()
        await reader.close()


# --- a database created BEFORE the change gains the column through the tool ---------------------------------------------------------------------

def test_the_pre_change_fixture_is_the_script_as_it_was_and_the_shipped_one_adds_the_column_behind_a_guard():
    text = FIXTURE.read_text()
    shipped = schema.read("20-credit-wallet.sql")

    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "e0785146c8455f03c372261ce99ff43a1735cc1502af386a9bc5c550e9599bb2"
    assert "request" not in text.split("CREATE TABLE IF NOT EXISTS credit_transactions")[1].split(");")[0] and "ADD COLUMN request" not in text
    assert "ADD COLUMN request JSONB" in shipped and "pg_attribute" in shipped, "added behind a catalog check"
    assert "ADD COLUMN IF NOT EXISTS request" not in shipped, "a plain IF NOT EXISTS would take ACCESS EXCLUSIVE on the hot table at every re-apply"


async def test_a_database_made_from_the_old_script_is_upgraded_by_migrate_keeping_every_old_row(container_url, monkeypatch):
    database = await create_database(container_url)
    try:
        # 1. the old schema, exactly as it was, with a wallet written by the old code (no `request` column existed)
        old_sql = "\n".join(line for line in FIXTURE.read_text().splitlines() if not line.startswith("\\connect")) + "\n"
        async with database.connection() as conn:
            await conn.execute(old_sql)
            await conn.execute(schema.executable("22-billing.sql"))
        async with database.connection() as conn:
            await conn.execute("INSERT INTO credit_accounts (tenant, created_by) VALUES ('acme', 'old')")
            await conn.execute(
                "INSERT INTO credit_transactions (tenant, kind, idempotency_key, reason, actor) VALUES ('acme', 'grant', 'old-key', 'r', 'a')"
            )
            await conn.execute(
                "INSERT INTO credit_lots (tenant, source, granted, remaining, created_by) VALUES ('acme', 'manual', 100, 100, 'old')"
            )
        assert await database.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'credit_transactions' AND column_name = 'request'"
        ) == 0
        old_rows = await database.rows("SELECT id, tenant, kind, idempotency_key, reason, actor FROM credit_transactions")

        # 2. the migration tool has recorded the OLD script, as a deployed service would have
        monkeypatch.setattr(migrate, "get_connection", database.connection)
        real_shipped, real_statements = migrate.shipped, migrate.statements
        old = migrate.Script("20-credit-wallet.sql", hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        monkeypatch.setattr(migrate, "shipped", lambda: [old, *real_shipped()[1:]])
        monkeypatch.setattr(migrate, "statements", lambda name: old_sql if name == old.name else real_statements(name))
        await migrate.migrate()
        monkeypatch.setattr(migrate, "shipped", real_shipped)
        monkeypatch.setattr(migrate, "statements", real_statements)

        # 3. the package is upgraded: the wallet script is now `changed`, and migrate applies it
        status = await migrate.status()
        assert {s.name: s.state for s in status.scripts} == {"20-credit-wallet.sql": "changed", "22-billing.sql": "applied"}
        result = await migrate.migrate()

        assert result.applied == ["20-credit-wallet.sql"] and (await migrate.status()).up_to_date
        assert await database.rows(
            "SELECT data_type, is_nullable FROM information_schema.columns WHERE table_name = 'credit_transactions' AND column_name = 'request'"
        ) == [("jsonb", "YES")]
        assert await database.rows("SELECT id, tenant, kind, idempotency_key, reason, actor FROM credit_transactions") == old_rows, "every old row is intact"
        assert await database.scalar("SELECT request IS NULL FROM credit_transactions WHERE idempotency_key = 'old-key'") is True

        # 4. old keys behave as they always did; new writes record their request
        monkeypatch.setattr(credits, "get_connection", database.connection)
        replay = await grant("acme", key="old-key", amount=D("999"), source="manual", expires_at=None)
        fresh = await grant("acme", key="new-key", amount=D("7"), source="manual", expires_at=None, provider=None, external_ref=None)
        assert replay.status == "duplicate" and fresh.status == "applied"
        assert (await database.scalar("SELECT request->>'amount' FROM credit_transactions WHERE idempotency_key = 'new-key'")) == "7.000000"
        with pytest.raises(credits.IdempotencyConflict):
            await grant("acme", key="new-key", amount=D("8"), source="manual", expires_at=None, provider=None, external_ref=None)
    finally:
        await database.drop()


# --- simultaneous first uses, and tenant boundaries -----------------------------------------------------------------------------------------------

async def test_two_simultaneous_first_uses_of_one_key_with_different_requests_have_exactly_one_winner(wallet):
    for round_ in range(6):
        key = f"race-{round_}"
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                grant("acme", key=key, amount=D("500")), grant("acme", key=key, amount=D("600")), return_exceptions=True
            ),
            timeout=60,
        )

        winners = [o for o in outcomes if isinstance(o, credits.Applied) and o.status == "applied"]
        losers = [o for o in outcomes if isinstance(o, credits.IdempotencyConflict)]
        assert len(winners) == 1 and len(losers) == 1, f"round {round_}: {outcomes}"
        assert await wallet.scalar("SELECT count(*) FROM credit_transactions WHERE tenant = 'acme' AND idempotency_key = %s", key) == 1
        stored = await wallet.scalar("SELECT request->>'amount' FROM credit_transactions WHERE tenant = 'acme' AND idempotency_key = %s", key)
        assert stored == format(winners[0].amount, "f"), "the stored request is the winner's"
    assert await credits.verify("acme") == []


async def test_two_simultaneous_identical_first_uses_are_one_grant_and_one_duplicate(wallet):
    outcomes = await asyncio.wait_for(asyncio.gather(grant("acme", key="same"), grant("acme", key="same")), timeout=60)

    assert sorted(o.status for o in outcomes) == ["applied", "duplicate"]


async def test_the_same_key_in_two_tenants_never_conflicts_and_an_error_never_shows_another_tenants_request(wallet):
    await grant("acme", key="K", amount=D("500"), reason="acme-private-ticket")
    await grant("globex", key="K", amount=D("777"), reason="globex-private-ticket")

    with pytest.raises(credits.IdempotencyConflict) as raised:
        await grant("acme", key="K", amount=D("600"), reason="acme-private-ticket")

    text = str(raised.value) + repr(raised.value.differences)
    assert raised.value.tenant == "acme" and raised.value.differences == {"amount": ("500.000000", "600.000000")}
    assert "777" not in text and "globex" not in text, "globex's request is not visible from acme's key"
    assert (await grant("globex", key="K", amount=D("777"), reason="globex-private-ticket")).status == "duplicate"


async def test_the_wallet_stays_consistent_after_every_refusal_in_this_file(wallet):
    await grant("acme")
    for change in GRANT_CHANGE.values():
        with pytest.raises(credits.IdempotencyConflict):
            await grant("acme", **change)

    assert await credits.verify("acme") == []
    assert (await credits.balance("acme")).ledger == D("500")
    assert isinstance(wallet, Throwaway)
