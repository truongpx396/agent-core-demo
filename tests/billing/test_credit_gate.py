"""The credit gate: may this tenant start another turn on its prepaid balance? (app/agent/budgets.py,
specs/010 T016 and T017.)

The rule is a plain function of (ctx, limits, gate, wallet, holds), so each case below names exactly the
balance, the holds, the rate and the failure policy it is about, with the wallet and the ledger faked.
What the wallet's SQL does is proven against a real Postgres in
tests/integration/test_credit_charge_real_postgres.py, which also runs the whole path (a tenant with no
credits is refused with no model call; a grant lets the next turn through).

Three things matter most and each has its own class:
  * WHO is gated (only a tenant with a wallet; spec D8) and what counts as "has credit";
  * that enforcement OFF changes nothing, down to the wallet never being read (SC-005);
  * what happens when the wallet cannot be read (the failure policy, a decision, never a silent default).
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.agent import budgets, spend, usage_ledger
from app.agent import runtime as runtime_module
from app.agent import runtime_stream as stream_module
from app.billing import credits
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TENANT_DAY = budgets.BudgetLimit("tenant", "day", 10.0)
GATE = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1"), fail_policy="open")
CLOSED = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1"), fail_policy="closed")


def _balance(available) -> credits.Balance:
    available = D(str(available))
    return credits.Balance(available=available, ledger=available, debt=max(-available, D(0)))


@pytest.fixture
def world(monkeypatch):
    """A ledger with no spend, no holds, and a wallet the test sets; every read is recorded."""
    state = {"wallet": None, "wallet_error": None, "wallet_reads": [], "reserved_usd": 0.0, "hold_reads": 0, "spent": 0.0}

    async def account_balance(tenant):
        state["wallet_reads"].append(tenant)
        if state["wallet_error"]:
            raise state["wallet_error"]
        return state["wallet"]

    async def usage_summary(tenant, principal=None, since=None):
        return {"total_cost_usd": state["spent"], "total_tokens": 0}

    async def in_flight_reservation(tenant):
        state["hold_reads"] += 1
        return state["reserved_usd"]

    monkeypatch.setattr(credits, "account_balance", account_balance)
    monkeypatch.setattr(spend, "usage_summary", usage_summary)
    monkeypatch.setattr(usage_ledger, "in_flight_reservation", in_flight_reservation)
    return state


async def _check(gate=GATE, ctx=TEST_CTX, fail_policy="open", limits=()):
    """No dollar limits unless a test names them: the tenant's own limit also reads the in-flight holds,
    which would blur a test's count of what the CREDIT gate read."""
    return await budgets.check_allowance(ctx, limits=list(limits), fail_policy=fail_policy, now=NOW, credit_gate=gate)


def _refused_count() -> float:
    return metric_value(metrics.agent_credit_enforcement_refused_total)


def _degraded(path: str) -> float:
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


class TestWhoIsGatedAndWhatCountsAsCredit:
    async def test_a_tenant_with_no_wallet_is_never_gated(self, world):
        """Spec D8: shipping the gate changes nothing for a tenant until a wallet is opened for it."""
        world["wallet"] = None
        before = _refused_count()

        allowance = await _check()

        assert allowance.status == "ok" and not allowance.refused
        assert _refused_count() == before
        assert world["hold_reads"] == 0, "a tenant with no wallet needs no hold arithmetic"

    async def test_a_tenant_with_credits_is_served(self, world):
        world["wallet"] = _balance(100)

        assert (await _check()).status == "ok"

    async def test_a_tenant_with_a_wallet_and_nothing_in_it_is_refused(self, world):
        """Unlike a tenant with NO wallet: an account at zero is the prepaid customer who ran out."""
        world["wallet"] = _balance(0)
        before = _refused_count()

        allowance = await _check()

        assert allowance.status == "insufficient_credits" and allowance.refused
        assert _refused_count() == before + 1
        assert (allowance.available_credits, allowance.reserved_credits) == (D("0"), D("0"))

    async def test_a_tenant_in_debt_is_refused_until_it_is_repaid(self, world):
        world["wallet"] = _balance(-25)

        allowance = await _check()

        assert allowance.status == "insufficient_credits" and allowance.available_credits == D("-25")

    async def test_the_smallest_positive_balance_is_enough(self, world):
        world["wallet"] = _balance("0.000001")

        assert (await _check()).status == "ok"


class TestHoldsAreInCredits:
    async def test_in_flight_turns_are_converted_at_the_configured_rate(self, world):
        """One turn in flight holds MAX_COST_USD_PER_TURN dollars; at 1000 credits per dollar, $0.50 is 500
        credits, so a tenant with 100 cannot start a second concurrent turn that may spend 500."""
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.50

        allowance = await _check()

        assert allowance.status == "insufficient_credits"
        assert allowance.reserved_credits == D("500.000000")

    async def test_a_small_hold_leaves_the_tenant_able_to_start_a_turn(self, world):
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.05  # 50 credits

        assert (await _check()).status == "ok"

    async def test_the_markup_applies_to_holds_as_it_does_to_charges(self, world):
        """A hold that ignored the markup would let a marked-up tenant start turns it cannot pay for."""
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.05  # 50 credits at cost, 75 with a 1.5x markup
        marked_up = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1.5"), fail_policy="open")

        allowance = await _check(gate=marked_up)

        assert allowance.status == "ok"
        world["reserved_usd"] = 0.0667  # 66.7 credits at cost, 100.05 with the markup
        assert (await _check(gate=marked_up)).status == "insufficient_credits"

    async def test_a_balance_exactly_used_up_by_holds_is_not_positive_so_it_is_refused(self, world):
        world["wallet"] = _balance(50)
        world["reserved_usd"] = 0.05  # exactly 50 credits

        assert (await _check()).status == "insufficient_credits"


class TestOrder:
    async def test_a_dollar_limit_that_is_used_up_refuses_first_and_the_wallet_is_never_read(self, world):
        """An operator's spend cap is what the caller is told about: buying credits would not help a tenant
        the cap is stopping. It also means a refused turn pays for no extra read."""
        world["spent"] = 10.0
        world["wallet"] = _balance(0)

        allowance = await _check(limits=[TENANT_DAY])

        assert allowance.status == "exceeded"
        assert world["wallet_reads"] == []

    async def test_the_wallet_is_read_exactly_once_for_a_turn_the_limits_would_serve(self, world):
        world["wallet"] = _balance(5)

        await _check()

        assert world["wallet_reads"] == [TEST_CTX["tenant"]]

    async def test_the_wallet_read_is_scoped_to_the_callers_own_tenant(self, world):
        world["wallet"] = _balance(5)
        other = {"tenant": "globex", "principal": "bob", "claims": {}}

        await _check(ctx=other)

        assert world["wallet_reads"] == ["globex"]

    async def test_an_unattributable_caller_is_not_gated_and_nothing_is_read(self, world):
        """Same rule as every other limit: nothing to meter means nothing to refuse."""
        for ctx in (None, {"tenant": "", "principal": "", "claims": {}}):
            assert (await _check(ctx=ctx)).status == "ok"

        assert world["wallet_reads"] == []


class TestEnforcementOffChangesNothing:
    """SC-005. Without a gate the wallet is not so much ignored as never reached."""

    async def test_a_tenant_with_no_credits_is_served_and_the_wallet_is_never_read(self, world):
        world["wallet"] = _balance(0)
        before = _refused_count()

        allowance = await _check(gate=None)

        assert allowance.status == "ok" and not allowance.degraded
        assert world["wallet_reads"] == [] and world["hold_reads"] == 0
        assert _refused_count() == before

    async def test_a_wallet_that_would_fail_to_read_cannot_affect_a_turn(self, world):
        world["wallet_error"] = ConnectionError("wallet down")
        before = _degraded("credit_read")

        allowance = await _check(gate=None)

        assert allowance.status == "ok" and not allowance.degraded
        assert _degraded("credit_read") == before

    async def test_the_runtime_builds_no_gate_when_enforcement_is_off(self, monkeypatch):
        monkeypatch.setattr(runtime_module, "CREDITS_ENFORCEMENT", False)
        monkeypatch.setattr(runtime_module, "CREDITS_PER_USD", D("1000"))  # shadow mode: a rate, no gate

        assert runtime_module._credit_gate() is None

    async def test_the_default_allowance_through_the_runtime_never_reads_the_wallet(self, world):
        world["wallet"] = _balance(0)

        assert await runtime_module._allowance_refusal(TEST_CTX) is None
        assert world["wallet_reads"] == []


class TestWhenTheWalletCannotBeRead:
    async def test_open_serves_the_turn_counts_it_and_marks_it_unverified(self, world):
        world["wallet_error"] = ConnectionError("wallet unreachable")
        before = _degraded("credit_read")

        allowance = await _check(gate=GATE)

        assert allowance.status == "ok" and allowance.degraded is True
        assert _degraded("credit_read") == before + 1

    async def test_closed_refuses_the_turn_as_unavailable_and_still_counts_it(self, world):
        world["wallet_error"] = ConnectionError("wallet unreachable")
        before = _degraded("credit_read")

        allowance = await _check(gate=CLOSED)

        assert allowance.status == "unavailable" and allowance.refused
        assert _degraded("credit_read") == before + 1
        envelope = budgets.refusal_envelope(allowance)
        assert envelope.code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE, "it blames no one's balance"

    async def test_the_ledger_policy_does_not_decide_the_wallets(self, world):
        """The two can fail separately, so each has its own policy: an open ledger policy with a closed
        wallet policy must refuse."""
        world["wallet_error"] = ConnectionError("down")

        allowance = await _check(gate=CLOSED, fail_policy="open")

        assert allowance.status == "unavailable"

    async def test_a_degraded_pass_is_not_confused_with_a_refusal_by_another_limit(self, world):
        world["wallet_error"] = ConnectionError("down")
        world["spent"] = 10.0  # the dollar limit refuses first

        allowance = await _check(gate=GATE, limits=[TENANT_DAY])

        assert allowance.status == "exceeded" and world["wallet_reads"] == []

    async def test_a_failed_hold_read_fails_open_to_no_holds_and_is_counted(self, world, monkeypatch):
        """The hold read only closes a race between concurrent turns (as for the dollar limits), so it
        fails open either way; the real function swallows its own error, so use it against a dead database."""
        monkeypatch.setattr(usage_ledger, "in_flight_reservation", _REAL_IN_FLIGHT)  # conftest's autouse mock refuses the connection
        world["wallet"] = _balance(5)
        before = _degraded("reservation")

        allowance = await _check(gate=CLOSED)

        assert allowance.status == "ok"
        assert _degraded("reservation") == before + 1


_REAL_IN_FLIGHT = usage_ledger.in_flight_reservation


class TestTheRefusal:
    def test_it_has_its_own_code_a_caller_can_switch_on(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("insufficient_credits", available_credits=D("0")))

        assert envelope.code == errors.ErrorCode.INSUFFICIENT_CREDITS
        assert envelope.code.value == "insufficient_credits"
        assert envelope.to_dict()["details"] == {"scope": "tenant"}

    def test_it_does_not_echo_a_balance(self):
        """The balance is GET /usage's to report; an error that echoed it would put it in every log that
        keeps error text."""
        envelope = budgets.refusal_envelope(
            budgets.Allowance("insufficient_credits", available_credits=D("-1234.5"), reserved_credits=D("987"))
        )

        assert "1234" not in envelope.message and "987" not in envelope.message
        assert "1234" not in str(envelope.details)

    def test_it_is_not_one_of_the_budget_codes_so_existing_clients_are_unaffected(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("insufficient_credits"))

        assert envelope.code not in {
            errors.ErrorCode.TENANT_BUDGET_EXCEEDED,
            errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED,
            errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE,
        }


class TestTheRuntimeBinding:
    async def test_enforcement_builds_a_gate_from_the_configured_rate_markup_and_policy(self, monkeypatch):
        monkeypatch.setattr(runtime_module, "CREDITS_ENFORCEMENT", True)
        monkeypatch.setattr(runtime_module, "CREDITS_PER_USD", D("250"))
        monkeypatch.setattr(runtime_module, "MARKUP", D("1.2"))
        monkeypatch.setattr(runtime_module, "CREDIT_CHECK_FAILURE_POLICY", "closed")

        assert runtime_module._credit_gate() == budgets.CreditGate(D("250"), D("1.2"), "closed")

    async def test_enforcement_without_a_rate_is_a_loud_error_never_a_silent_no_gate(self, monkeypatch):
        """Settings refuse this at startup; this is for a caller that re-points the globals into a state
        they forbid. Quietly skipping the gate would serve a tenant the operator meant to stop."""
        monkeypatch.setattr(runtime_module, "CREDITS_ENFORCEMENT", True)
        monkeypatch.setattr(runtime_module, "CREDITS_PER_USD", None)

        with pytest.raises(RuntimeError, match="CREDITS_PER_USD"):
            runtime_module._credit_gate()

    async def test_the_refusal_every_entry_point_asks_for_carries_the_credit_code(self, world, monkeypatch):
        monkeypatch.setattr(runtime_module, "CREDITS_ENFORCEMENT", True)
        monkeypatch.setattr(runtime_module, "CREDITS_PER_USD", D("1000"))
        world["wallet"] = _balance(0)

        envelope = await runtime_module._allowance_refusal(TEST_CTX)

        assert envelope is not None and envelope.code == errors.ErrorCode.INSUFFICIENT_CREDITS


class _GraphTouched(AssertionError):
    pass


class TestEntryPointsRefuseBeforeAnyModelWork:
    """A new turn and a resume both ask `_allowance_refusal`, so both refuse a tenant with no credits
    before the graph is initialised, and neither takes a hold."""

    @pytest.fixture(autouse=True)
    def enforcing_with_an_empty_wallet(self, world, monkeypatch):
        monkeypatch.setattr(runtime_module, "CREDITS_ENFORCEMENT", True)
        monkeypatch.setattr(runtime_module, "CREDITS_PER_USD", D("1000"))
        world["wallet"] = _balance(0)
        reserved = []

        async def boom(*args, **kwargs):
            raise _GraphTouched("the graph must not be touched for a tenant with no credits")

        async def reserve(ctx):
            reserved.append(ctx)
            return "hold"

        monkeypatch.setattr(runtime_module, "init_graph_async", boom)
        monkeypatch.setattr(runtime_module, "_reserve_turn_budget", reserve)
        self.reserved = reserved
        self.world = world

    async def test_a_new_turn_is_refused_with_the_credit_code(self):
        events = [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        assert len(events) == 1 and events[0]["type"] == "error"
        assert events[0]["code"] == errors.ErrorCode.INSUFFICIENT_CREDITS.value
        assert self.reserved == []

    async def test_a_resume_is_refused_with_the_credit_code(self):
        events = [event async for event in stream_module.astream_events_resume("t1", True, TEST_CTX)]

        assert len(events) == 1 and events[0]["code"] == errors.ErrorCode.INSUFFICIENT_CREDITS.value
        assert self.reserved == []

    async def test_a_grant_lets_the_next_turn_through_to_the_graph(self):
        """US3's independent test, hermetically: the same call, now with credits, reaches graph work."""
        self.world["wallet"] = _balance(500)

        with pytest.raises(_GraphTouched):
            [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]
