"""Recording billable usage against a REAL Postgres (sql/24 and 25, credit_billing/usage.py; specs/013): the
guarantees the agent app proved of its own table (specs/010 T015/T023), now proved of the package's.
tests/test_usage_record.py proves which statements run, on which connection and savepoint, through a fake;
only the DATABASE shows what it does with them (constitution VII):
- the event, its debit and its export row are ONE transaction: a replayed call is one event and one debit, and
  a rollback takes all three;
- `PRIMARY KEY (tenant, event_id)` arbitrates concurrent writers, and the tenant is part of the key (two
  tenants may share an id);
- a debit that blows up part-way is rolled back by its SAVEPOINT without taking the event, leaving no orphan
  transaction row;
- 50 concurrent recordings to one tenant lose no update (SC-003 of spec 010);
- the table is append-only, refuses credits without a rate, and has exactly ONE unique constraint (a second
  would make `ON CONFLICT` unreliable under concurrency, which bit the host's table);
- the script can be applied twice, and the outbox can only carry its own tenant's event.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors as pg_errors

from credit_billing import credits, store, usage
from credit_billing.usage import Usage
from tests.conftest import metric_value
from tests.containers import ensure_postgres
from tests.integration.schema_reapply import reapply

pytestmark = pytest.mark.integration

D = Decimal


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    monkeypatch.setattr(credits, "get_connection", get_connection)
    monkeypatch.setattr(usage, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage, "MARKUP", D("1"))
    monkeypatch.setattr(usage, "BILLING_PROVIDERS", ())


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


async def record(url: str, tenant: str, event_id: str | None = None, cost_usd: float | None = 0.01, kind: str = "chat") -> usage.Recorded:
    """One recording in its own transaction, exactly as a host does: it commits when the block exits."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await usage.record_usage_in(conn, Usage(tenant, event_id or uuid.uuid4().hex, kind, cost_usd, model="gpt", tokens=150))


async def fetch(url: str, sql: str, *params) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def scalar(url: str, sql: str, *params):
    return (await fetch(url, sql, *params))[0][0]


async def fund(tenant: str, amount="1000") -> None:
    await credits.grant(tenant, amount, source="purchase", idempotency_key=f"g-{tenant}", actor="test")


class TestTheEventAndItsDebitAreOneTransaction:
    async def test_a_call_is_one_event_rated_in_credits_and_one_debit_that_names_it(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await record(appdata_url, tenant, "m1", cost_usd=0.01)  # 0.01 USD x 1000 = 10 credits

        ((cost, rated, rate, markup, model, tokens, kind),) = await fetch(
            appdata_url, "SELECT cost_usd, credits, credits_per_usd, markup, model, tokens, kind FROM billing_usage_events WHERE tenant = %s", tenant
        )
        assert (cost, rated, rate, markup) == (D("0.010000000000"), D("10.000000"), D("1000.000000"), D("1.000000"))
        assert (model, tokens, kind) == ("gpt", 150, "chat")
        ((tx_kind, key, linked, tx_cost, tx_rate),) = await fetch(
            appdata_url,
            "SELECT kind, idempotency_key, usage_event_id, cost_usd, credits_per_usd FROM credit_transactions WHERE tenant = %s AND kind = 'debit'",
            tenant,
        )
        assert (tx_kind, key, linked) == ("debit", "m1", "m1")
        assert (tx_cost, tx_rate) == (D("0.010000000000"), D("1000.000000")), "the debit records what it was priced from"
        assert (await credits.balance(tenant)).available == D("990.000000")
        assert await credits.verify(tenant) == []

    async def test_replaying_the_same_call_neither_adds_an_event_nor_charges_twice(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        results = [await record(appdata_url, tenant, "same-message") for _ in range(3)]

        assert [r.status for r in results] == ["recorded", "duplicate", "duplicate"]
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_twenty_concurrent_writers_of_one_call_charge_it_once(self, appdata_url):
        """The race a retried worker and a replayed turn would create: the primary key decides who inserts, and only the inserter debits."""
        tenant = tenant_name()
        await fund(tenant, "1000")

        await asyncio.gather(*[record(appdata_url, tenant, "contended") for _ in range(20)])

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_fifty_concurrent_calls_lose_no_update(self, appdata_url):
        """SC-003, end to end: fifty different calls for one tenant, each its own event and debit."""
        tenant = tenant_name()
        await fund(tenant, "10000")

        await asyncio.gather(*[record(appdata_url, tenant, f"m{i}") for i in range(50)])

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 50
        balance = await credits.balance(tenant)
        assert balance.available == D("10000") - 50 * D("10"), "exactly fifty debits of ten credits"
        assert await credits.verify(tenant) == []
        entries = await scalar(appdata_url, "SELECT COALESCE(SUM(amount), 0) FROM credit_entries WHERE tenant = %s", tenant)
        assert entries == balance.ledger, "balance == SUM(entries)"

    async def test_two_tenants_may_use_the_same_event_id_and_each_is_charged_once(self, appdata_url):
        """The tenant is part of the key. (A usage-billing provider deduplicates on the id alone, which is why the export contract asks for ids that
        are unique across tenants; the record itself does not need that.)"""
        a, b = tenant_name(), tenant_name()
        await fund(a, "100")
        await fund(b, "100")

        await record(appdata_url, a, "shared-id")
        await record(appdata_url, b, "shared-id")

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE event_id = 'shared-id' AND tenant IN (%s, %s)", a, b) == 2
        assert (await credits.balance(a)).available == D("90.000000") and (await credits.balance(b)).available == D("90.000000")

    async def test_a_rollback_of_the_callers_transaction_takes_the_event_and_the_debit_with_it(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT 1")  # the caller's own statement opens its transaction, as a host's insert of its own event does
            await usage.record_usage_in(conn, Usage(tenant, "rolled-back", "chat", 0.01))
            await conn.rollback()

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 0
        assert (await credits.balance(tenant)).available == D("1000.000000")

    async def test_a_tenant_with_no_wallet_gets_its_event_and_nothing_else(self, appdata_url):
        """No account means never debited, and a charge must not OPEN an account either."""
        tenant = tenant_name()

        await record(appdata_url, tenant, "m1")

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_accounts WHERE tenant = %s", tenant) == 0
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s", tenant) == 0

    async def test_usage_beyond_the_balance_is_booked_as_debt_never_refused(self, appdata_url):
        """The work has already happened, so the debit records it: gating is a separate check."""
        tenant = tenant_name()
        await fund(tenant, "4")

        await record(appdata_url, tenant, "m1", cost_usd=0.01)  # 10 credits against 4

        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("-6.000000"), D("6.000000"))

    async def test_an_unpriced_call_is_recorded_with_null_credits_and_charges_nothing(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await record(appdata_url, tenant, "m1", cost_usd=None)

        ((cost, rated, rate),) = await fetch(
            appdata_url, "SELECT cost_usd, credits, credits_per_usd FROM billing_usage_events WHERE tenant = %s", tenant
        )
        assert (cost, rated) == (None, None), "unknown stays unknown"
        assert rate == D("1000.000000"), "the rate in force is still recorded"
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0
        assert (await credits.balance(tenant)).available == D("1000.000000")

    async def test_a_free_call_is_a_real_zero_and_writes_no_debit(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await record(appdata_url, tenant, "m1", cost_usd=0.0)

        assert await scalar(appdata_url, "SELECT credits FROM billing_usage_events WHERE tenant = %s", tenant) == D("0")
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0

    async def test_a_later_rate_change_never_rewrites_what_a_past_call_was_charged(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        await fund(tenant, "100000")

        await record(appdata_url, tenant, "before")  # at 1000/USD: 10
        monkeypatch.setattr(usage, "CREDITS_PER_USD", D("2000"))
        monkeypatch.setattr(usage, "MARKUP", D("1.5"))
        await record(appdata_url, tenant, "after")  # at 2000/USD x 1.5: 30

        rows = await fetch(appdata_url, "SELECT credits, credits_per_usd, markup FROM billing_usage_events WHERE tenant = %s ORDER BY credits", tenant)
        assert rows == [(D("10.000000"), D("1000.000000"), D("1.000000")), (D("30.000000"), D("2000.000000"), D("1.500000"))]
        assert (await credits.balance(tenant)).available == D("100000") - 40

    async def test_a_cheap_call_is_charged_a_fraction_of_a_credit_not_nothing(self, appdata_url):
        """$0.0000004 at 1000 per dollar is 0.0004 credits: exact NUMERIC end to end, no float anywhere."""
        tenant = tenant_name()
        await fund(tenant, "1")

        await record(appdata_url, tenant, "m1", cost_usd=0.0000004)

        assert await scalar(appdata_url, "SELECT credits FROM billing_usage_events WHERE tenant = %s", tenant) == D("0.000400")
        assert (await credits.balance(tenant)).available == D("0.999600")

    async def test_with_nothing_billing_not_one_row_is_written(self, appdata_url, monkeypatch):
        monkeypatch.setattr(usage, "CREDITS_PER_USD", None)
        tenant = tenant_name()

        recorded = await record(appdata_url, tenant, "m1")

        assert recorded.status == "off"
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 0


class TestAWalletFaultNeverLosesTheEvent:
    @pytest.fixture
    def wallet_fault(self, monkeypatch):
        """A switch for a wallet that fails AFTER its debit has inserted its transaction row: the case a savepoint exists for, since it must undo
        that row and still leave the event. Off until a test has funded the wallet (a grant uses the same write)."""
        real_move = credits._move

        async def move_then_fail(conn, tenant, transaction_id, lot_id, amount):
            raise ConnectionError("the wallet fell over mid-debit")

        class Fault:
            def break_(self):
                monkeypatch.setattr(credits, "_move", move_then_fail)

            def heal(self):
                monkeypatch.setattr(credits, "_move", real_move)

        return Fault()

    async def test_the_event_stays_and_the_half_done_debit_leaves_nothing_behind(self, appdata_url, wallet_fault):
        tenant = tenant_name()
        await fund(tenant, "1000")
        wallet_fault.break_()
        before = metric_value(usage.metrics.agent_cost_governance_degraded_total, path="credit_debit")
        write_before = metric_value(usage.metrics.agent_cost_governance_degraded_total, path="billing_event_write")

        recorded = await record(appdata_url, tenant, "m1")  # does not raise

        assert recorded.status == "recorded" and recorded.debit is None
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1, "the record outranks the charge"
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0, (
            "the savepoint rolled the debit's own transaction row back: no orphan"
        )
        wallet_fault.heal()
        assert (await credits.balance(tenant)).available == D("1000.000000")
        assert await credits.verify(tenant) == []
        assert metric_value(usage.metrics.agent_cost_governance_degraded_total, path="credit_debit") == before + 1
        assert metric_value(usage.metrics.agent_cost_governance_degraded_total, path="billing_event_write") == write_before

    async def test_the_missed_charge_can_be_replayed_from_the_event_alone(self, appdata_url, wallet_fault):
        """The claim the design rests on: an uncharged event is repairable because the debit key IS the event id and the row holds every figure the
        debit needs. (`credit-billing-reconcile` is what names the gap; this proves the repair works, not that it is scheduled.)"""
        tenant = tenant_name()
        await fund(tenant, "1000")
        wallet_fault.break_()
        await record(appdata_url, tenant, "m1")
        wallet_fault.heal()  # the wallet is healthy again

        ((event_id, rated, cost, rate, markup, kind),) = await fetch(
            appdata_url, "SELECT event_id, credits, cost_usd, credits_per_usd, markup, kind FROM billing_usage_events WHERE tenant = %s", tenant
        )
        repair = dict(usage_event_id=event_id, actor="system", reason=f"usage ({kind})", pricing=credits.Pricing(cost, rate, markup))
        applied = await credits.debit(tenant, rated, idempotency_key=event_id, **repair)
        again = await credits.debit(tenant, rated, idempotency_key=event_id, **repair)  # the repair run twice: the SAME request

        assert applied.status == "applied" and again.status == "duplicate"
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_a_missing_event_table_is_counted_and_never_raised(self, appdata_url, monkeypatch):
        """An unapplied schema must not take the caller down: it is the counted, paged path."""
        tenant = tenant_name()
        before = metric_value(usage.metrics.agent_cost_governance_degraded_total, path="billing_event_write")
        monkeypatch.setattr(usage, "_INSERT", usage._INSERT.replace("billing_usage_events", "billing_usage_events_that_does_not_exist"))

        recorded = await record(appdata_url, tenant, "m1")

        assert recorded.status == "failed"
        assert metric_value(usage.metrics.agent_cost_governance_degraded_total, path="billing_event_write") == before + 1


class TestTheSchemaGuardsTheRecord:
    async def test_credits_without_a_rate_are_refused_by_the_database(self, appdata_url):
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute("INSERT INTO billing_usage_events (tenant, event_id, kind, credits) VALUES (%s, %s, 'chat', 5)", (tenant_name(), uuid.uuid4().hex))

    @pytest.mark.parametrize("column,value", [("credits", -1), ("credits_per_usd", 0), ("markup", -0.5), ("cost_usd", -1), ("tokens", -1)])
    async def test_a_nonsense_figure_is_refused(self, appdata_url, column, value):
        row = {"credits": 5, "credits_per_usd": 1000, "markup": 1, "cost_usd": 1, "tokens": 1, column: value}
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO billing_usage_events (tenant, event_id, kind, credits, credits_per_usd, markup, cost_usd, tokens) "
                    "VALUES (%s, %s, 'chat', %s, %s, %s, %s, %s)",
                    (tenant_name(), uuid.uuid4().hex, row["credits"], row["credits_per_usd"], row["markup"], row["cost_usd"], row["tokens"]),
                )

    @pytest.mark.parametrize("column", ["tenant", "event_id", "kind"])
    async def test_a_blank_identity_is_refused(self, appdata_url, column):
        values = {"tenant": tenant_name(), "event_id": uuid.uuid4().hex, "kind": "chat", column: ""}
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute("INSERT INTO billing_usage_events (tenant, event_id, kind) VALUES (%s, %s, %s)", (values["tenant"], values["event_id"], values["kind"]))

    async def test_the_table_is_append_only_an_update_is_always_refused(self, appdata_url):
        tenant = tenant_name()
        await record(appdata_url, tenant, "m1")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await conn.execute("UPDATE billing_usage_events SET credits = 0 WHERE tenant = %s", (tenant,))

    async def test_a_delete_is_refused_unless_the_transaction_says_it_is_the_retention_job(self, appdata_url):
        tenant = tenant_name()
        await record(appdata_url, tenant, "m1")
        await record(appdata_url, tenant, "m2")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await conn.execute("DELETE FROM billing_usage_events WHERE tenant = %s", (tenant,))
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT set_config('billing_usage_events.allow_delete', 'on', true)")
            cur = await conn.execute("DELETE FROM billing_usage_events WHERE tenant = %s AND event_id = 'm1'", (tenant,))
            assert cur.rowcount == 1
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException):
                await conn.execute("DELETE FROM billing_usage_events WHERE tenant = %s", (tenant,))  # the permission was local to that transaction

    async def test_the_table_has_exactly_one_unique_constraint_its_composite_primary_key(self, appdata_url):
        """A second one would let two writers of the same event collide on the constraint that is NOT the `ON CONFLICT` arbiter, and Postgres would raise
        instead of doing nothing (found on the host's table by a test failing 3 runs in 40)."""
        rows = await fetch(
            appdata_url,
            "SELECT i.indisprimary, array_agg(a.attname ORDER BY a.attnum) FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
            "WHERE i.indrelid = 'billing_usage_events'::regclass AND i.indisunique GROUP BY i.indexrelid, i.indisprimary",
        )
        assert rows == [(True, ["tenant", "event_id"])]

    async def test_the_scripts_can_be_applied_twice(self, appdata_url):
        """An operator re-running them by hand (or `credit-billing-migrate` on every start) against a volume that already has them must not fail."""
        await reapply(appdata_url, "24-billing-usage-events.sql")
        await reapply(appdata_url, "25-billing-export-outbox.sql")


class TestQueuedWithTheEvent:
    """The outbox row is written by the real `usage.record_usage_in`, in the event's own transaction."""

    @pytest.fixture
    def exporting(self, monkeypatch):
        provider = f"p-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(usage.providers, "usage_export_providers", lambda names: (provider,))
        monkeypatch.setattr(usage, "BILLING_PROVIDERS", (provider,))
        return provider

    async def link(self, url: str, tenant: str, provider: str) -> None:
        async with await psycopg.AsyncConnection.connect(url) as conn:
            await store.link_customer(conn, tenant, provider, f"cus_{uuid.uuid4().hex[:8]}")

    async def test_a_linked_tenants_event_is_queued_for_its_provider_in_the_same_transaction(self, appdata_url, exporting):
        tenant = tenant_name()
        await self.link(appdata_url, tenant, exporting)

        await record(appdata_url, tenant, "ev-1")

        rows = await fetch(appdata_url, "SELECT provider, tenant, event_id, status FROM billing_export_outbox WHERE tenant = %s", tenant)
        assert rows == [(exporting, tenant, "ev-1", "pending")]

    async def test_a_tenant_with_no_link_gets_its_event_and_no_outbox_row(self, appdata_url, exporting):
        tenant = tenant_name()

        await record(appdata_url, tenant, "ev-1")

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_export_outbox WHERE tenant = %s", tenant) == 0

    async def test_a_replay_queues_nothing_twice(self, appdata_url, exporting):
        tenant = tenant_name()
        await self.link(appdata_url, tenant, exporting)

        for _ in range(3):
            await record(appdata_url, tenant, "ev-1")

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_export_outbox WHERE tenant = %s", tenant) == 1

    async def test_a_rollback_takes_the_event_and_its_export_row_together(self, appdata_url, exporting):
        tenant = tenant_name()
        await self.link(appdata_url, tenant, exporting)

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT 1")  # the caller's own statement opens its transaction
            await usage.record_usage_in(conn, Usage(tenant, "ev-1", "chat", 0.01))
            await conn.rollback()

        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 0
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_export_outbox WHERE tenant = %s", tenant) == 0

    async def test_a_failure_to_queue_keeps_the_event_and_is_counted(self, appdata_url, exporting, monkeypatch):
        tenant = tenant_name()
        await self.link(appdata_url, tenant, exporting)
        monkeypatch.setattr(usage, "_ENQUEUE", "INSERT INTO billing_export_outbox_that_does_not_exist (provider) VALUES (1)")
        before = metric_value(usage.metrics.agent_cost_governance_degraded_total, path="export_enqueue")

        recorded = await record(appdata_url, tenant, "ev-1")

        assert recorded.status == "recorded"
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert metric_value(usage.metrics.agent_cost_governance_degraded_total, path="export_enqueue") == before + 1

    async def test_an_export_row_can_only_carry_its_own_tenants_event(self, appdata_url, exporting):
        """The composite foreign key (tenant, event_id): the old table needed a trigger for this."""
        owner, other = tenant_name(), tenant_name()
        await record(appdata_url, owner, "ev-1")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.ForeignKeyViolation):
                await conn.execute("INSERT INTO billing_export_outbox (provider, tenant, event_id) VALUES (%s, %s, 'ev-1')", (exporting, other))

    async def test_an_event_with_an_export_row_cannot_be_deleted_out_from_under_it(self, appdata_url, exporting):
        tenant = tenant_name()
        await self.link(appdata_url, tenant, exporting)
        await record(appdata_url, tenant, "ev-1")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT set_config('billing_usage_events.allow_delete', 'on', true)")
            with pytest.raises(pg_errors.ForeignKeyViolation):
                await conn.execute("DELETE FROM billing_usage_events WHERE tenant = %s", (tenant,))
