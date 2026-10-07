"""A model call is rated in credits and its wallet debited in the event's own transaction
(app/agent/usage_events.py, specs/010 T015).

What is proven HERE, against a fake connection, is the shape of the thing: which statements run, on which
connection, in what order, inside which savepoint, and what is counted when the wallet fails. What
the database then DOES with them (one debit however many times an event is written, an atomic commit, a
savepoint that rolls back only the debit, 50 concurrent charges losing nothing) is real-Postgres behaviour:
tests/integration/test_credit_charge_real_postgres.py. A fake cannot show it and these tests do not
claim to.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from psycopg import errors as pg_errors

from app.agent import pricing, usage_events
from app.agent.pricing import ModelPrice, PricedCall
from app.billing import credits
from app.core import metrics
from tests.conftest import TEST_CTX, metric_value

_REAL_INSERT = usage_events._insert  # imported before any autouse fixture patches it

D = Decimal


def _priced(cost_usd: float | None = 0.0123, tokens: int = 150) -> PricedCall:
    price = ModelPrice(0.0001, 0.0002) if cost_usd is not None else None
    return PricedCall(100, tokens - 100, 0, tokens, cost_usd, price)


def _count(path: str) -> float:
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


@pytest.fixture
def rate(monkeypatch):
    """Credits on at 1000 per dollar, marked up 1.5x."""
    monkeypatch.setattr(usage_events, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage_events, "MARKUP", D("1.5"))


class TestRating:
    def test_with_no_rate_there_is_nothing_to_add_so_the_row_is_what_it_was_before_credits_existed(self, monkeypatch):
        monkeypatch.setattr(usage_events, "CREDITS_PER_USD", None)

        assert usage_events._rating(_priced()) == {}

    def test_credits_are_cost_times_rate_times_markup_and_the_rate_is_stored_with_them(self, rate):
        rating = usage_events._rating(_priced(0.0123))

        assert rating["credits"] == D("18.450000")  # 0.0123 x 1000 x 1.5
        assert (rating["credits_per_usd"], rating["markup"]) == (D("1000"), D("1.5"))

    def test_the_credits_are_reproducible_from_the_row_alone(self, rate):
        """The stored cost is what is multiplied (not a differently-rounded one), so a reconciliation can
        recompute any event's credits from its own columns."""
        rating = usage_events._rating(_priced(0.0123456789012345))

        assert rating["cost_usd"] == D("0.012345678901")  # the column's twelve places
        assert rating["credits"] == credits.credits_for_cost(rating["cost_usd"], rating["credits_per_usd"], rating["markup"])

    def test_a_call_costing_a_fraction_of_a_millionth_of_a_dollar_is_still_charged_something(self, rate):
        """$0.0000004 x 1000 x 1.5 = 0.0006 credits. Rounding the cost to six places first would charge 0."""
        assert usage_events._rating(_priced(0.0000004))["credits"] == D("0.000600")

    def test_an_unpriced_call_has_unknown_credits_never_zero(self, rate):
        rating = usage_events._rating(_priced(None))

        assert rating["credits"] is None
        assert "cost_usd" not in rating, "the NULL cost stays NULL; it is not replaced by a number"
        assert (rating["credits_per_usd"], rating["markup"]) == (D("1000"), D("1.5"))

    def test_a_genuinely_free_call_is_a_real_zero(self, rate):
        assert usage_events._rating(_priced(0.0))["credits"] == D("0.000000")


class FakeConnection:
    """Records every statement, every savepoint, and the order they happened in."""

    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount
        self.log: list[tuple] = []

    async def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
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

    monkeypatch.setattr(usage_events.credits, "debit_in", debit_in)
    return spy


@pytest.fixture
def connection(monkeypatch):
    """Patches `usage_events.get_connection` to hand out ONE connection per `async with`; the list
    collects them so a test can assert there was exactly one (one transaction)."""
    handed_out: list[FakeConnection] = []
    config = SimpleNamespace(rowcount=1)

    @asynccontextmanager
    async def get_connection():
        conn = FakeConnection(config.rowcount)
        handed_out.append(conn)
        yield conn

    monkeypatch.setattr(usage_events, "get_connection", get_connection)
    return SimpleNamespace(handed_out=handed_out, config=config)


def _rated_row(**overrides) -> dict:
    row = {
        "event_id": "e1", "tenant": "acme", "principal": "alice", "thread_id": "t", "kind": "chat",
        "model_alias": "chat", "resolved_model": None, "input_tokens": 100, "output_tokens": 50,
        "cached_input_tokens": 0, "total_tokens": 150, "cost_usd": D("0.0123"),
        "price_input_per_token": 0.0001, "price_output_per_token": 0.0002,
        "credits": D("18.45"), "credits_per_usd": D("1000"), "markup": D("1.5"),
    }
    row.update(overrides)
    return row


class TestTheChargeIsPartOfTheEventsTransaction:
    async def test_a_rated_event_is_inserted_then_debited_on_the_same_connection(self, connection, wallet):
        assert await _REAL_INSERT(_rated_row()) is True

        assert len(connection.handed_out) == 1, "one connection is one transaction: the insert and the debit commit together"
        assert wallet.connections == connection.handed_out, "the debit ran on the connection the insert used"
        kinds = [entry[0] for entry in connection.handed_out[0].log]
        assert kinds == ["execute", "savepoint_begin", "debit", "savepoint_end"]

    async def test_the_insert_names_the_credit_columns(self, connection, wallet):
        await _REAL_INSERT(_rated_row())

        _, sql, params = connection.handed_out[0].log[0]
        assert "credits, credits_per_usd, markup" in sql and "ON CONFLICT (event_id) DO NOTHING" in sql
        assert params["credits"] == D("18.45")

    async def test_the_debit_is_keyed_by_the_event_id_and_carries_the_pricing_it_was_computed_from(self, connection, wallet):
        await _REAL_INSERT(_rated_row())

        (tenant, amount, kwargs), = wallet.calls
        assert (tenant, amount) == ("acme", D("18.45"))
        assert kwargs["idempotency_key"] == "e1", "a replayed call can only ever be one debit"
        assert kwargs["usage_event_id"] == "e1"
        assert kwargs["pricing"] == credits.Pricing(cost_usd=D("0.0123"), credits_per_usd=D("1000"), markup=D("1.5"))

    async def test_a_duplicate_event_is_never_charged_again(self, connection, wallet):
        connection.config.rowcount = 0  # ON CONFLICT DO NOTHING: the event was already there

        assert await _REAL_INSERT(_rated_row()) is False

        assert wallet.calls == []
        assert [entry[0] for entry in connection.handed_out[0].log] == ["execute"]

    async def test_an_unpriced_event_is_recorded_but_debits_nothing(self, connection, wallet):
        assert await _REAL_INSERT(_rated_row(credits=None, cost_usd=None)) is True

        assert wallet.calls == [], "unknown is never silently free, and never a made-up charge either"
        assert [entry[0] for entry in connection.handed_out[0].log] == ["execute"]

    async def test_without_a_rate_the_original_statement_runs_alone(self, connection, wallet):
        """SC-005: a deployment with credits off sends exactly the statement it always did, with no
        extra round trip, no savepoint, and no mention of a column it may not have."""
        row = {k: v for k, v in _rated_row().items() if k not in ("credits", "credits_per_usd", "markup")}

        assert await _REAL_INSERT(row) is True

        (entry,) = connection.handed_out[0].log
        assert entry[0] == "execute" and "credits" not in entry[1]
        assert wallet.calls == []


class TestAWalletFaultNeverLosesTheEventAndIsNeverSilent:
    async def test_a_failing_debit_is_swallowed_counted_and_the_event_still_stands(self, connection, wallet):
        wallet.error = ConnectionError("wallet unreachable")
        before = _count("credit_debit")
        write_before = _count("usage_event_write")

        assert await _REAL_INSERT(_rated_row()) is True, "the event was written; only its charge failed"

        assert _count("credit_debit") == before + 1
        assert _count("usage_event_write") == write_before, "this is not a lost event, so it must not page as one"

    async def test_only_the_debit_is_inside_the_savepoint_so_the_insert_is_never_rolled_back_with_it(self, connection, wallet):
        wallet.error = ConnectionError("boom")

        await _REAL_INSERT(_rated_row())

        log = connection.handed_out[0].log
        assert [entry[0] for entry in log] == ["execute", "savepoint_begin", "debit", "savepoint_end"]
        assert log[-1] == ("savepoint_end", "ConnectionError"), "the error reached the savepoint, which rolls back to it"

    async def test_a_missing_wallet_table_is_the_same_counted_path(self, connection, wallet):
        wallet.error = pg_errors.UndefinedTable('relation "credit_accounts" does not exist')
        before = _count("credit_debit")

        assert await _REAL_INSERT(_rated_row()) is True

        assert _count("credit_debit") == before + 1


class TestMissingCreditColumns:
    async def test_an_unapplied_migration_21_is_the_counted_missing_table_path_not_a_write_failure(self, monkeypatch, caplog):
        async def missing(row):
            raise pg_errors.UndefinedColumn('column "credits" of relation "usage_events" does not exist')

        monkeypatch.setattr(usage_events, "_insert", missing)
        before = _count("usage_event_table_missing")
        write_before = _count("usage_event_write")

        with caplog.at_level("WARNING", logger=usage_events.logger.name):
            await usage_events.record_call(
                TEST_CTX, thread_id="t", message_id="m", kind="chat", model_alias="chat", priced=_priced()
            )

        assert _count("usage_event_table_missing") == before + 1
        assert _count("usage_event_write") == write_before
        assert any("21-usage-event-credits.sql" in r.getMessage() for r in caplog.records), "it must say what to apply"


class TestRecordCallEndToEnd:
    """`record_call` -> the real `_insert` -> the wallet spy: the whole path a model call takes."""

    @pytest.fixture(autouse=True)
    def real_insert(self, monkeypatch):
        monkeypatch.setattr(usage_events, "_insert", _REAL_INSERT)

    async def test_a_priced_call_is_rated_stored_with_its_rate_and_charged(self, rate, connection, wallet):
        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id="m1", kind="chat", model_alias="chat", priced=_priced(0.0123)
        )

        _, sql, params = connection.handed_out[0].log[0]
        assert params["credits"] == D("18.450000") and params["credits_per_usd"] == D("1000") and params["markup"] == D("1.5")
        (tenant, amount, kwargs), = wallet.calls
        assert (tenant, amount) == (TEST_CTX["tenant"], D("18.450000"))
        assert kwargs["idempotency_key"] == params["event_id"]

    async def test_an_unpriced_call_debits_nothing_and_is_counted_as_unpriced_exactly_once(
        self, rate, connection, wallet, monkeypatch
    ):
        async def no_price(alias):
            return None

        monkeypatch.setattr(pricing, "get_price", no_price)
        pricing.reset_pricing_state()
        before = metric_value(metrics.agent_unpriced_usage_total, model_alias="chat")
        priced = await pricing.price_call("chat", {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})

        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id="m2", kind="chat", model_alias="chat", priced=priced
        )

        assert priced.cost_usd is None
        assert wallet.calls == []
        _, _, params = connection.handed_out[0].log[0]
        assert params["credits"] is None and params["cost_usd"] is None
        assert metric_value(metrics.agent_unpriced_usage_total, model_alias="chat") == before + 1

    async def test_with_credits_off_a_call_writes_the_row_it_always_did_and_never_touches_the_wallet(
        self, monkeypatch, connection, wallet
    ):
        monkeypatch.setattr(usage_events, "CREDITS_PER_USD", None)

        await usage_events.record_call(
            TEST_CTX, thread_id="t", message_id="m3", kind="chat", model_alias="chat", priced=_priced()
        )

        (entry,) = connection.handed_out[0].log
        assert "credits" not in entry[2] and wallet.calls == []
