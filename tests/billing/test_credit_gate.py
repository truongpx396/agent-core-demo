"""The credit gate as the agent app uses it: may this tenant start another turn on its prepaid balance? (app/agent/cost/budgets.py, specs/010 T016 and
T017; the decision moved into the billing package by specs/013.)

The DECISION (who is gated, what counts as credit, holds in credits, the failure policy) is the package's and is proven there
(packages/billing/tests/test_admission.py). What is proven HERE is the agent's side of it:

  * the agent maps each of the gate's four answers onto its own refusal, and a refusal by a dollar limit comes first (the order);
  * with enforcement off the gate answers without reading a wallet, through the real package;
  * the refusal carries its own error code and does not echo a balance;
  * every entry point asks, so a tenant with no credits is refused before any model work, and a grant lets the next turn through.
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.agent.cost import budget_holds, budgets, spend
from app.agent.runtime import core as runtime_module
from app.agent.runtime import stream as stream_module
from app.billing_integration import port as billing_port
from app.core import errors, metrics
from credit_billing import admission, credits
from tests.conftest import TEST_CTX, metric_value

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TENANT_DAY = budgets.BudgetLimit("tenant", "day", 10.0)


class FakeGate:
    """Implements the port's `admit` and records every question; the rest of the port is never reached by the gate."""

    def __init__(self) -> None:
        self.answer = billing_port.Admission("allowed")
        self.asked: list[str] = []

    async def record_usage_in(self, conn, usage):  # pragma: no cover - the gate never records usage
        raise AssertionError("the gate must not record usage")

    async def account_balance(self, tenant):  # pragma: no cover
        raise AssertionError("the gate asks admit, never the balance")

    async def credit_summary(self, tenant):  # pragma: no cover
        raise AssertionError("the gate does not show a summary")

    async def admit(self, tenant, *, reserved_usd):
        self.asked.append(tenant)
        return self.answer


@pytest.fixture
def world(monkeypatch):
    """A ledger with no spend and no holds, and a gate whose answer the test sets; every question is recorded."""
    state = {"spent": 0.0, "gate": FakeGate()}

    async def usage_summary(tenant, principal=None, since=None):
        return {"total_cost_usd": state["spent"], "total_tokens": 0}

    async def in_flight_reservation(tenant):
        return 0.0

    monkeypatch.setattr(spend, "usage_summary", usage_summary)
    monkeypatch.setattr(budget_holds, "in_flight_reservation", in_flight_reservation)
    monkeypatch.setattr(billing_port, "billing", state["gate"])
    return state


async def _check(ctx=TEST_CTX, fail_policy="open", limits=()):
    """No dollar limits unless a test names them: the tenant's own limit also reads the in-flight holds, which would blur a test's count."""
    return await budgets.check_allowance(ctx, limits=list(limits), fail_policy=fail_policy, now=NOW)


class TestTheAgentMapsEachAnswerOntoItsOwnRefusal:
    async def test_allowed_serves_the_turn(self, world):
        allowance = await _check()

        assert allowance.status == "ok" and not allowance.refused and not allowance.degraded

    async def test_exhausted_refuses_before_any_model_work_with_the_figures_the_gate_gave(self, world):
        world["gate"].answer = billing_port.Admission("exhausted", D("-25"), D("500"))

        allowance = await _check()

        assert allowance.status == "insufficient_credits" and allowance.refused
        assert (allowance.available_credits, allowance.reserved_credits) == (D("-25"), D("500"))

    async def test_unavailable_refuses_and_blames_no_ones_balance(self, world):
        world["gate"].answer = billing_port.Admission("unavailable")

        allowance = await _check()

        assert allowance.status == "unavailable" and allowance.refused
        assert budgets.refusal_envelope(allowance).code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE

    async def test_degraded_serves_the_turn_and_marks_it_unverified(self, world):
        world["gate"].answer = billing_port.Admission("degraded")

        allowance = await _check()

        assert allowance.status == "ok" and allowance.degraded is True

    async def test_the_ledger_policy_does_not_decide_the_wallets(self, world):
        """The two can fail separately, so each has its own policy: an open ledger policy with a gate that says unavailable must refuse."""
        world["gate"].answer = billing_port.Admission("unavailable")

        assert (await _check(fail_policy="open")).status == "unavailable"


class TestOrder:
    async def test_a_dollar_limit_that_is_used_up_refuses_first_and_the_gate_is_never_asked(self, world):
        """An operator's spend cap is what the caller is told about: buying credits would not help a tenant the cap is stopping. It also means a
        refused turn pays for no extra read."""
        world["spent"] = 10.0
        world["gate"].answer = billing_port.Admission("exhausted", D("0"), D("0"))

        allowance = await _check(limits=[TENANT_DAY])

        assert allowance.status == "exceeded"
        assert world["gate"].asked == []

    async def test_the_gate_is_asked_exactly_once_for_a_turn_the_limits_would_serve(self, world):
        await _check()

        assert world["gate"].asked == [TEST_CTX["tenant"]]

    async def test_the_question_is_scoped_to_the_callers_own_tenant(self, world):
        other = {"tenant": "globex", "principal": "bob", "claims": {}}

        await _check(ctx=other)

        assert world["gate"].asked == ["globex"]

    async def test_an_unattributable_caller_is_not_gated_and_nothing_is_asked(self, world):
        """Same rule as every other limit: nothing to meter means nothing to refuse."""
        for ctx in (None, {"tenant": "", "principal": "", "claims": {}}):
            assert (await _check(ctx=ctx)).status == "ok"

        assert world["gate"].asked == []

    async def test_a_degraded_pass_is_not_confused_with_a_refusal_by_another_limit(self, world):
        world["gate"].answer = billing_port.Admission("degraded")
        world["spent"] = 10.0  # the dollar limit refuses first

        allowance = await _check(limits=[TENANT_DAY])

        assert allowance.status == "exceeded" and world["gate"].asked == []


class TestEnforcementOffChangesNothing:
    """Spec 010 SC-005, through the REAL package: with enforcement off the wallet is not so much ignored as never reached."""

    @pytest.fixture
    def real_gate(self, monkeypatch):
        monkeypatch.setattr(billing_port, "billing", billing_port.InProcessBilling())
        reads = []

        async def account_balance(tenant):
            reads.append(tenant)
            return credits.Balance(D("0"), D("0"), D("0"))

        monkeypatch.setattr(credits, "account_balance", account_balance)
        monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", False)
        monkeypatch.setattr(admission, "CREDITS_PER_USD", D("1000"))  # shadow mode: a rate, no gate
        return reads

    async def test_a_tenant_with_no_credits_is_served_and_the_wallet_is_never_read(self, world, real_gate):
        before = metric_value(admission.metrics.agent_credit_enforcement_refused_total)

        allowance = await _check()

        assert allowance.status == "ok" and not allowance.degraded
        assert real_gate == []
        assert metric_value(admission.metrics.agent_credit_enforcement_refused_total) == before

    async def test_the_default_allowance_through_the_runtime_never_reads_the_wallet(self, world, real_gate):
        assert await runtime_module._allowance_refusal(TEST_CTX) is None
        assert real_gate == []

    async def test_with_enforcement_on_the_same_wallet_refuses(self, world, real_gate, monkeypatch):
        monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", True)

        allowance = await _check()

        assert allowance.status == "insufficient_credits" and real_gate == [TEST_CTX["tenant"]]


class TestTheHoldReadFailsOpen:
    async def test_a_failed_hold_read_is_counted_and_reads_as_no_holds_when_the_gate_asks_for_it(self, world, monkeypatch):
        """The hold read only closes a race between concurrent turns (as for the dollar limits), so it fails open either way; the real function
        swallows its own error, so use it against a dead database, through a gate that asks for it."""
        monkeypatch.setattr(budget_holds, "in_flight_reservation", _REAL_IN_FLIGHT)  # conftest's autouse mock refuses the connection
        seen = []

        class AsksForHolds(FakeGate):
            async def admit(self, tenant, *, reserved_usd):
                seen.append(await reserved_usd())
                return billing_port.Admission("allowed")

        monkeypatch.setattr(billing_port, "billing", AsksForHolds())
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="reservation")

        allowance = await _check()

        assert allowance.status == "ok" and seen == [0.0]
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="reservation") == before + 1


_REAL_IN_FLIGHT = budget_holds.in_flight_reservation


class TestTheRefusal:
    def test_it_has_its_own_code_a_caller_can_switch_on(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("insufficient_credits", available_credits=D("0")))

        assert envelope.code == errors.ErrorCode.INSUFFICIENT_CREDITS
        assert envelope.code.value == "insufficient_credits"
        assert envelope.to_dict()["details"] == {"scope": "tenant"}

    def test_it_does_not_echo_a_balance(self):
        """The balance is GET /usage's to report; an error that echoed it would put it in every log that keeps error text."""
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
    async def test_the_refusal_every_entry_point_asks_for_carries_the_credit_code(self, world):
        world["gate"].answer = billing_port.Admission("exhausted", D("0"), D("0"))

        envelope = await runtime_module._allowance_refusal(TEST_CTX)

        assert envelope is not None and envelope.code == errors.ErrorCode.INSUFFICIENT_CREDITS

    async def test_the_runtime_knows_no_rate_markup_or_policy_because_the_gate_is_not_its_business(self):
        for name in ("CREDITS_PER_USD", "MARKUP", "CREDITS_ENFORCEMENT", "CREDIT_CHECK_FAILURE_POLICY", "_credit_gate"):
            assert not hasattr(runtime_module, name), name


class _GraphTouched(AssertionError):
    pass


class TestEntryPointsRefuseBeforeAnyModelWork:
    """A new turn and a resume both ask `_allowance_refusal`, so both refuse a tenant with no credits before the graph is initialised, and neither
    takes a hold."""

    @pytest.fixture(autouse=True)
    def an_empty_wallet(self, world, monkeypatch):
        world["gate"].answer = billing_port.Admission("exhausted", D("0"), D("0"))
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
        self.world["gate"].answer = billing_port.Admission("allowed")

        with pytest.raises(_GraphTouched):
            [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]
