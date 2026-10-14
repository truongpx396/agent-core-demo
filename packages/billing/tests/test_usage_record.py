"""Recording billable usage (credit_billing/usage.py, specs/013): what is proven HERE, against a fake connection, is the shape of the thing:
which statements run, on which connection, in what order, inside which savepoint, and what is counted when a part fails. What the database
then DOES with them (one row and one debit however many times an event is recorded, an atomic commit, a savepoint that rolls back only the
debit, 50 concurrent recordings losing nothing, the outbox row's tenant) is real-Postgres behaviour: tests/integration/test_usage_real_postgres.py.
A fake cannot show it and these tests do not claim to.

These are the guarantees the agent app's `test_charge_on_event.py` and `test_export_enqueue.py` proved of its own table, moved here with the code.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from psycopg import errors as pg_errors

from credit_billing import credits, providers, usage
from credit_billing.usage import Usage
from tests.conftest import metric_value

D = Decimal


def _count(path: str) -> float:
    return metric_value(usage.metrics.agent_cost_governance_degraded_total, path=path)


def _event(**overrides) -> Usage:
    values = {"tenant": "acme", "event_id": "e1", "kind": "chat", "cost_usd": 0.0123, "model": "gpt", "tokens": 150}
    values.update(overrides)
    return Usage(**values)


class FakeConnection:
    """Records every statement, every savepoint, and the order they happened in."""

    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount
        self.log: list[tuple] = []
        self.fail_on: str | None = None
        self.fail_with: Exception = ConnectionError("unreachable")

    async def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        if self.fail_on and self.fail_on in sql:
            raise self.fail_with
        return SimpleNamespace(rowcount=self.rowcount)

    def transaction(self):
        conn = self

        class Savepoint:
            async def __aenter__(self):
                conn.log.append(("savepoint_begin",))

            async def __aexit__(self, exc_type, exc, tb):
                conn.log.append(("savepoint_end", exc_type.__name__ if exc_type else None))
                return False  # an exception is NOT swallowed by the savepoint itself

        return Savepoint()

    @property
    def kinds(self) -> list[str]:
        return [entry[0] for entry in self.log]


@pytest.fixture
def rate(monkeypatch):
    """Credits on at 1000 per dollar, marked up 1.5x, and no provider that bills on usage."""
    monkeypatch.setattr(usage, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage, "MARKUP", D("1.5"))
    monkeypatch.setattr(usage, "BILLING_PROVIDERS", ())


@pytest.fixture
def exporting(monkeypatch):
    """One enabled provider that bills on usage, and no rate."""
    monkeypatch.setattr(usage, "CREDITS_PER_USD", None)
    monkeypatch.setattr(usage, "BILLING_PROVIDERS", ("fake",))
    assert providers.usage_export_providers(("fake",)) == ("fake",)


@pytest.fixture
def wallet(monkeypatch):
    """A spy standing in for `credits.debit_in`; set `.error` to make the wallet fail."""
    spy = SimpleNamespace(calls=[], error=None, connections=[])

    async def debit_in(conn, tenant, amount, **kwargs):
        spy.calls.append((tenant, amount, kwargs))
        spy.connections.append(conn)
        conn.log.append(("debit",))
        if spy.error is not None:
            raise spy.error
        return credits.Applied("applied")

    monkeypatch.setattr(credits, "debit_in", debit_in)  # resolved at call time
    return spy


class TestRating:
    async def test_credits_are_cost_times_rate_times_markup_and_the_rate_is_stored_with_them(self, rate, wallet):
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event(cost_usd=0.0123))

        _, _, params = conn.log[1]
        assert params["credits"] == D("18.450000") and recorded.credits == D("18.450000")  # 0.0123 x 1000 x 1.5
        assert (params["credits_per_usd"], params["markup"]) == (D("1000"), D("1.5"))

    async def test_the_credits_are_reproducible_from_the_row_alone(self, rate, wallet):
        """The stored cost is what is multiplied (not a differently-rounded one), so a reconciliation can recompute any event's credits."""
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event(cost_usd=0.0123456789012345))

        params = conn.log[1][2]
        assert params["cost_usd"] == D("0.012345678901"), "the column's twelve places"
        assert params["credits"] == credits.credits_for_cost(params["cost_usd"], params["credits_per_usd"], params["markup"])

    async def test_a_call_costing_a_fraction_of_a_millionth_of_a_dollar_is_still_charged_something(self, rate, wallet):
        """$0.0000004 x 1000 x 1.5 = 0.0006 credits. Rounding the cost to six places first would charge 0."""
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event(cost_usd=0.0000004))

        assert conn.log[1][2]["credits"] == D("0.000600")

    async def test_an_unpriced_call_has_unknown_credits_never_zero_and_debits_nothing(self, rate, wallet):
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event(cost_usd=None))

        params = conn.log[1][2]
        assert (params["cost_usd"], params["credits"]) == (None, None), "the NULL cost stays NULL; it is not replaced by a number"
        assert (params["credits_per_usd"], params["markup"]) == (D("1000"), D("1.5")), "the rate in force is still recorded"
        assert wallet.calls == [] and recorded.status == "recorded" and recorded.debit is None
        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_end"]

    async def test_a_genuinely_free_call_is_a_real_zero_that_debits_nothing(self, rate, wallet):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event(cost_usd=0.0))

        assert conn.log[1][2]["credits"] == D("0.000000")
        (call,) = wallet.calls
        assert call[1] == D("0.000000"), "debit_in itself answers 'nothing' for a zero; the caller does not special-case it"

    async def test_a_float_is_read_as_a_person_wrote_it_not_as_its_binary_value(self, rate, wallet):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event(cost_usd=0.1))

        assert conn.log[1][2]["cost_usd"] == D("0.100000000000")


class TestTheEventAndItsChargeAreOneTransaction:
    async def test_the_event_is_inserted_then_debited_on_the_same_connection_inside_one_outer_savepoint(self, rate, wallet):
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event())

        assert recorded.status == "recorded"
        assert wallet.connections == [conn], "the debit ran on the connection the insert used"
        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_begin", "debit", "savepoint_end", "savepoint_end"]

    async def test_the_insert_names_the_composite_key_and_carries_the_tenant(self, rate, wallet):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event(tenant="acme"))

        _, sql, params = conn.log[1]
        assert "ON CONFLICT (tenant, event_id) DO NOTHING" in sql, "the primary key is (tenant, event_id): that is the arbiter"
        assert params["tenant"] == "acme" and params["event_id"] == "e1"

    async def test_the_debit_is_keyed_by_the_event_id_and_carries_the_pricing_it_was_computed_from(self, rate, wallet):
        await usage.record_usage_in(FakeConnection(), _event())

        ((tenant, amount, kwargs),) = wallet.calls
        assert (tenant, amount) == ("acme", D("18.450000"))
        assert kwargs["idempotency_key"] == "e1", "a replayed call can only ever be one debit"
        assert kwargs["usage_event_id"] == "e1"
        assert kwargs["pricing"] == credits.Pricing(cost_usd=D("0.012300000000"), credits_per_usd=D("1000"), markup=D("1.5"))
        assert kwargs["reason"] == "usage (chat)"

    async def test_a_duplicate_event_is_never_charged_or_queued_again(self, rate, wallet):
        conn = FakeConnection(rowcount=0)  # ON CONFLICT DO NOTHING: the event was already there

        recorded = await usage.record_usage_in(conn, _event())

        assert recorded.status == "duplicate" and recorded.credits is None
        assert wallet.calls == []
        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_end"]


class TestWhenNothingIsBilling:
    async def test_with_no_rate_and_no_usage_billing_provider_not_one_statement_runs(self, wallet, monkeypatch):
        """A deployment that has not set billing up pays nothing: no round trip, no savepoint, no mention of a table it may not have."""
        monkeypatch.setattr(usage, "CREDITS_PER_USD", None)
        monkeypatch.setattr(usage, "BILLING_PROVIDERS", ())
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event())

        assert recorded.status == "off" and conn.log == [] and wallet.calls == []

    async def test_with_a_provider_that_only_sells_packs_there_is_still_nothing_to_record(self, wallet, monkeypatch):
        class WebhookOnly:
            name = "packs"
            capabilities = frozenset()

        monkeypatch.setitem(providers.FACTORIES, "packs", WebhookOnly)
        monkeypatch.setattr(usage, "CREDITS_PER_USD", None)
        monkeypatch.setattr(usage, "BILLING_PROVIDERS", ("packs",))
        conn = FakeConnection()

        assert (await usage.record_usage_in(conn, _event())).status == "off" and conn.log == []

    async def test_with_only_an_exporting_provider_the_event_is_kept_without_credits_and_nothing_is_debited(self, exporting, wallet):
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event())

        params = conn.log[1][2]
        assert (params["credits"], params["credits_per_usd"], params["markup"]) == (None, None, None)
        assert recorded.status == "recorded" and wallet.calls == []


class TestAWalletFaultNeverLosesTheEventAndIsNeverSilent:
    async def test_a_failing_debit_is_swallowed_counted_and_the_event_still_stands(self, rate, wallet):
        wallet.error = ConnectionError("wallet unreachable")
        before, write_before = _count("credit_debit"), _count("billing_event_write")

        recorded = await usage.record_usage_in(FakeConnection(), _event())

        assert recorded.status == "recorded", "the event was recorded; only its charge failed"
        assert recorded.debit is None and recorded.credits == D("18.450000"), "the credits it was worth are on the row, so the missing charge is visible"
        assert _count("credit_debit") == before + 1
        assert _count("billing_event_write") == write_before, "this is not a lost event, so it must not page as one"

    async def test_only_the_debit_is_inside_its_own_savepoint_so_the_insert_is_never_rolled_back_with_it(self, rate, wallet):
        wallet.error = ConnectionError("boom")
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event())

        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_begin", "debit", "savepoint_end", "savepoint_end"]
        assert conn.log[4] == ("savepoint_end", "ConnectionError"), "the error reached the inner savepoint, which rolls back to it"
        assert conn.log[5] == ("savepoint_end", None), "and the outer one, which holds the event, committed"

    async def test_a_missing_wallet_table_is_the_same_counted_path(self, rate, wallet):
        wallet.error = pg_errors.UndefinedTable('relation "credit_accounts" does not exist')
        before = _count("credit_debit")

        assert (await usage.record_usage_in(FakeConnection(), _event())).status == "recorded"

        assert _count("credit_debit") == before + 1


class TestAFailureToWriteTheEventIsCountedNotRaised:
    async def test_a_database_fault_is_counted_as_a_lost_billing_record_and_never_raised(self, rate, wallet):
        conn = FakeConnection()
        conn.fail_on = "INSERT INTO billing_usage_events"
        before = _count("billing_event_write")

        recorded = await usage.record_usage_in(conn, _event())

        assert recorded.status == "failed" and wallet.calls == []
        assert _count("billing_event_write") == before + 1
        assert conn.log[-1] == ("savepoint_end", "ConnectionError"), "everything this call wrote is undone with its savepoint"

    async def test_an_unapplied_schema_is_the_counted_path_and_says_what_to_apply_once(self, rate, wallet, caplog):
        usage.reset_state()
        conn = FakeConnection()
        conn.fail_on = "INSERT INTO billing_usage_events"
        conn.fail_with = pg_errors.UndefinedTable('relation "billing_usage_events" does not exist')
        before = _count("billing_event_write")

        with caplog.at_level("WARNING", logger=usage.logger.name):
            await usage.record_usage_in(conn, _event())
            await usage.record_usage_in(FakeConnection_failing(), _event())

        assert _count("billing_event_write") == before + 2
        said = [r.getMessage() for r in caplog.records if "billing_usage_events is missing" in r.getMessage()]
        assert len(said) == 1 and "credit-billing-migrate" in said[0], "the hint is given once per process, not on every call"
        usage.reset_state()


def FakeConnection_failing() -> FakeConnection:
    conn = FakeConnection()
    conn.fail_on = "INSERT INTO billing_usage_events"
    conn.fail_with = pg_errors.UndefinedTable("x")
    return conn


class TestInvalidInputIsCountedAndRefusedBeforeAnyStatement:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tenant", ""), ("tenant", "  "), ("event_id", ""), ("kind", ""), ("kind", None),
            ("tokens", -1), ("tokens", True), ("tokens", 1.5),
            ("cost_usd", -0.01), ("cost_usd", float("nan")), ("cost_usd", float("inf")), ("cost_usd", "not a number"),
        ],
    )
    async def test_a_bad_field_is_refused_without_touching_the_database(self, rate, wallet, field, value):
        conn = FakeConnection()
        before = _count("billing_event_write")

        recorded = await usage.record_usage_in(conn, _event(**{field: value}))

        assert recorded.status == "failed" and conn.log == [] and wallet.calls == []
        assert _count("billing_event_write") == before + 1

    async def test_a_negative_cost_is_refused_even_when_nothing_would_rate_it(self, exporting, wallet):
        """With only an exporting provider there is no rate, so no multiplication ever sees the cost: the refusal must come from the check itself,
        not from a later step that happens to trip over it (a negative cost stored as usage would be sent to a provider as a refund)."""
        conn = FakeConnection()

        recorded = await usage.record_usage_in(conn, _event(cost_usd=-0.01))

        assert recorded.status == "failed" and conn.log == []

    async def test_the_log_names_the_field_and_never_the_value(self, rate, wallet, caplog):
        with caplog.at_level("WARNING", logger=usage.logger.name):
            await usage.record_usage_in(FakeConnection(), _event(tenant="", event_id="secret-event-id"))

        (record,) = [r for r in caplog.records if r.getMessage() == "billing_usage_rejected"]
        assert record.reason == "tenant must be a non-blank string" and "secret-event-id" not in str(record.__dict__)


class TestQueuingForExport:
    @pytest.fixture
    def both(self, monkeypatch):
        monkeypatch.setattr(usage, "CREDITS_PER_USD", D("1000"))
        monkeypatch.setattr(usage, "MARKUP", D("1"))
        monkeypatch.setattr(usage, "BILLING_PROVIDERS", ("fake",))

    async def test_it_is_queued_on_the_same_connection_inside_a_savepoint_of_its_own(self, exporting):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event())

        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_begin", "execute", "savepoint_end", "savepoint_end"]
        _, sql, params = conn.log[3]
        assert "INSERT INTO billing_export_outbox" in sql and "ON CONFLICT (provider, tenant, event_id) DO NOTHING" in sql
        assert params == {"event_id": "e1", "tenant": "acme", "providers": ["fake"]}

    async def test_only_a_tenant_with_a_link_gets_a_row_because_the_insert_selects_from_the_links(self, exporting):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event())

        sql = conn.log[3][1]
        assert "FROM billing_customers" in sql and "c.tenant = %(tenant)s" in sql, "a tenant with no link matches no row"

    async def test_a_duplicate_event_queues_nothing(self, exporting):
        conn = FakeConnection(rowcount=0)

        assert (await usage.record_usage_in(conn, _event())).status == "duplicate"

        assert conn.kinds == ["savepoint_begin", "execute", "savepoint_end"]

    async def test_a_failure_to_queue_is_swallowed_counted_and_the_event_still_stands(self, exporting):
        conn = FakeConnection()
        conn.fail_on = "billing_export_outbox"
        before, write_before = _count("export_enqueue"), _count("billing_event_write")

        recorded = await usage.record_usage_in(conn, _event())

        assert recorded.status == "recorded", "the event was recorded; only its queuing failed"
        assert _count("export_enqueue") == before + 1
        assert _count("billing_event_write") == write_before, "this is not a lost event, so it must not page as one"
        assert conn.log[-2] == ("savepoint_end", "ConnectionError") and conn.log[-1] == ("savepoint_end", None)

    async def test_with_credits_and_export_both_on_the_charge_comes_first_then_the_queuing(self, both, wallet):
        conn = FakeConnection()

        await usage.record_usage_in(conn, _event())

        assert conn.kinds == [
            "savepoint_begin", "execute", "savepoint_begin", "debit", "savepoint_end", "savepoint_begin", "execute", "savepoint_end",
            "savepoint_end",
        ]
