"""The parts of the wallet that need no database: exact amounts, the allocation plan, and every
validation that must happen BEFORE any statement is sent (app/billing/credits.py).

What a wallet DOES (idempotency, locking, expiry, the schema's own guards) is real-Postgres behaviour and
is proven in tests/integration/test_credits_real_postgres.py; a fake cursor would only echo back
what was written. Here the arithmetic that decides who gets charged is pinned where it is cheap.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing import credits

D = Decimal


class TestCreditsAreExact:
    def test_a_float_is_read_through_its_shortest_repr_not_its_binary_value(self):
        assert credits.credits_amount(0.1) == D("0.100000")
        assert credits.credits_amount(0.1) + credits.credits_amount(0.2) == D("0.300000")  # a float sum is not 0.3

    def test_a_float_that_is_a_half_rounds_up_because_it_is_read_as_written(self):
        """0.0000005 as a float is 4.99999999999999977e-07 in binary, which would round DOWN. It is read
        through its shortest repr ('5e-07'), as a person wrote it, so it rounds half up."""
        assert credits.credits_amount(0.0000005) == D("0.000001")

    def test_it_rounds_half_up_at_six_places(self):
        assert credits.credits_amount("0.0000005") == D("0.000001")
        assert credits.credits_amount("0.0000004") == D("0.000000")

    def test_a_decimal_passes_through_quantised(self):
        assert credits.credits_amount(D("2")) == D("2.000000")

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN", "-Infinity"])
    def test_a_non_finite_amount_is_refused(self, bad):
        with pytest.raises(ValueError, match="finite"):
            credits.credits_amount(bad)


class TestCreditsForCost:
    """credits = round_half_up(cost_usd x credits_per_usd x markup, 6): the one formula a charge is priced by."""

    def test_a_cost_at_the_default_shape(self):
        assert credits.credits_for_cost(0.0123, 1000, 1) == D("12.300000")

    def test_a_markup_scales_it(self):
        assert credits.credits_for_cost(0.0123, 1000, D("1.5")) == D("18.450000")

    def test_a_tiny_call_is_charged_its_exact_fraction_not_a_whole_credit(self):
        """The reason credits are decimals: a $0.0000004 call at 1,000/USD is 0.0004 credits, not 1."""
        assert credits.credits_for_cost(0.0000004, 1000, 1) == D("0.000400")

    def test_a_free_call_costs_nothing(self):
        assert credits.credits_for_cost(0, 1000, 1) == D("0.000000")

    def test_it_rounds_half_up_at_six_places_of_credits(self):
        assert credits.credits_for_cost(D("0.0000000005"), 1000, 1) == D("0.000001")  # 0.0000005 credits

    def test_a_negative_or_non_finite_cost_is_refused(self):
        for bad in (-0.01, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="cost"):
                credits.credits_for_cost(bad, 1000, 1)

    def test_a_float_cost_is_read_as_written(self):
        assert credits.credits_for_cost(0.1 + 0.2, 10, 1) == D("3.000000")  # 0.30000000000000004 as written


class TestPlanAllocation:
    def test_an_exact_fit_takes_the_whole_lot_and_leaves_no_shortfall(self):
        assert credits.plan_allocation([("a", D("10"))], D("10")) == ([("a", D("10"))], D("0"))

    def test_a_partial_take_leaves_the_rest_of_the_lot(self):
        assert credits.plan_allocation([("a", D("10"))], D("4")) == ([("a", D("4"))], D("0"))

    def test_a_debit_spans_lots_in_the_order_given(self):
        takes, shortfall = credits.plan_allocation([("soon", D("3")), ("later", D("10"))], D("5"))

        assert takes == [("soon", D("3")), ("later", D("2"))]
        assert shortfall == D("0")

    def test_what_the_lots_cannot_cover_is_the_shortfall(self):
        takes, shortfall = credits.plan_allocation([("a", D("3")), ("b", D("2"))], D("10"))

        assert takes == [("a", D("3")), ("b", D("2"))]
        assert shortfall == D("5")

    def test_no_lots_means_the_whole_debit_is_shortfall(self):
        assert credits.plan_allocation([], D("7")) == ([], D("7"))

    def test_an_empty_or_negative_lot_is_skipped(self):
        takes, _ = credits.plan_allocation([("empty", D("0")), ("owed", D("-2")), ("real", D("5"))], D("3"))

        assert takes == [("real", D("3"))]

    def test_it_stops_as_soon_as_the_need_is_met(self):
        takes, _ = credits.plan_allocation([("a", D("5")), ("b", D("5"))], D("5"))

        assert takes == [("a", D("5"))]

    def test_taken_plus_shortfall_always_equals_the_need(self):
        for need in (D("0.000001"), D("4.5"), D("12"), D("99.999999")):
            takes, shortfall = credits.plan_allocation([("a", D("3")), ("b", D("4.25"))], need)
            assert sum((t for _, t in takes), D("0")) + shortfall == need


class _NoDatabase:
    """Any use fails the test: these calls must be refused before a statement is sent."""

    async def execute(self, *args, **kwargs):
        raise AssertionError("validation must happen before any SQL")


FUTURE = datetime.now(UTC) + timedelta(days=30)
GRANT = {"source": "purchase", "idempotency_key": "k1", "actor": "ops"}


class TestAGrantIsValidatedBeforeAnyStatement:
    @pytest.mark.parametrize("amount", [0, -5, "0.0000001"])
    async def test_it_must_be_above_zero(self, amount):
        with pytest.raises(ValueError, match="above zero"):
            await credits.grant_in(_NoDatabase(), "acme", amount, **GRANT)

    @pytest.mark.parametrize("source", ["overdraft", "gift", ""])
    async def test_an_unknown_source_is_refused_and_overdraft_is_never_grantable(self, source):
        with pytest.raises(ValueError, match="source"):
            await credits.grant_in(_NoDatabase(), "acme", 5, **{**GRANT, "source": source})

    async def test_it_needs_a_tenant(self):
        with pytest.raises(ValueError, match="tenant"):
            await credits.grant_in(_NoDatabase(), "", 5, **GRANT)

    @pytest.mark.parametrize("missing", ["idempotency_key", "actor"])
    async def test_it_needs_a_key_and_an_actor(self, missing):
        with pytest.raises(ValueError, match="actor"):
            await credits.grant_in(_NoDatabase(), "acme", 5, **{**GRANT, missing: ""})

    @pytest.mark.parametrize("source", ["promo", "subscription"])
    async def test_promotional_and_subscription_credits_must_expire(self, source):
        """Decision D9: a promo is a liability to cap in time, a subscription's credits do not roll over."""
        with pytest.raises(ValueError, match="must expire"):
            await credits.grant_in(_NoDatabase(), "acme", 5, **{**GRANT, "source": source})

    @pytest.mark.parametrize("source", ["purchase", "manual", "adjustment"])
    async def test_paid_and_operator_credits_do_not_need_an_expiry(self, source):
        """Reaches the database (and so the stand-in's refusal): the expiry rule did not stop it first."""
        with pytest.raises(AssertionError, match="before any SQL"):
            await credits.grant_in(_NoDatabase(), "acme", 5, **{**GRANT, "source": source})

    async def test_an_expiry_in_the_past_is_refused(self):
        with pytest.raises(ValueError, match="future"):
            await credits.grant_in(_NoDatabase(), "acme", 5, expires_at=datetime.now(UTC) - timedelta(seconds=1), **GRANT)

    async def test_a_naive_expiry_is_refused(self):
        """Ambiguous about its zone, so it cannot be compared with now()."""
        with pytest.raises(ValueError, match="timezone"):
            await credits.grant_in(_NoDatabase(), "acme", 5, expires_at=datetime.now() + timedelta(days=1), **GRANT)


class TestADebitIsValidatedBeforeAnyStatement:
    async def test_a_negative_debit_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            await credits.debit_in(_NoDatabase(), "acme", -1, idempotency_key="e1")

    async def test_a_zero_debit_is_nothing_and_touches_nothing(self):
        """A free model costs zero credits: that is not an event to lock a tenant for."""
        result = await credits.debit_in(_NoDatabase(), "acme", 0, idempotency_key="e1")

        assert result.status == "nothing" and not result.applied

    @pytest.mark.parametrize("tenant, key", [("", "e1"), ("acme", "")])
    async def test_it_needs_a_tenant_and_a_key(self, tenant, key):
        with pytest.raises(ValueError, match="tenant and an idempotency key"):
            await credits.debit_in(_NoDatabase(), tenant, 1, idempotency_key=key)
