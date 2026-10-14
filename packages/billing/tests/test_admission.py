"""The credit gate: may this tenant start another unit of work on its prepaid balance? (credit_billing/admission.py, specs/010 T016-T017, moved
here by specs/013 from the agent app's `test_credit_gate.py`.)

The rule is a plain function of (tenant, wallet, holds, rate, markup, policy), so each case below names exactly the balance, the holds, the rate
and the failure policy it is about, with the wallet faked. What the wallet's SQL does is proven against a real Postgres in the integration tier.

Three things matter most and each has its own class: WHO is gated (only a tenant with a wallet) and what counts as "has credit"; that enforcement
OFF changes nothing, down to the wallet never being read; and what happens when the wallet cannot be read (the failure policy, a decision, never
a silent default).
"""
from decimal import Decimal

import pytest

from credit_billing import admission, credits
from tests.conftest import metric_value

D = Decimal


def _balance(available) -> credits.Balance:
    available = D(str(available))
    return credits.Balance(available=available, ledger=available, debt=max(-available, D(0)))


@pytest.fixture
def world(monkeypatch):
    """Enforcing at 1000 credits per dollar, markup 1, policy open; a wallet the test sets; every read is recorded."""
    state = {"wallet": None, "wallet_error": None, "wallet_reads": [], "reserved_usd": 0.0, "hold_reads": 0}

    async def account_balance(tenant):
        state["wallet_reads"].append(tenant)
        if state["wallet_error"]:
            raise state["wallet_error"]
        return state["wallet"]

    async def reserved_usd():
        state["hold_reads"] += 1
        return state["reserved_usd"]

    monkeypatch.setattr(credits, "account_balance", account_balance)
    monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", True)
    monkeypatch.setattr(admission, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(admission, "MARKUP", D("1"))
    monkeypatch.setattr(admission, "CREDIT_CHECK_FAILURE_POLICY", "open")
    state["reserved"] = reserved_usd
    return state


async def _admit(world, tenant="acme"):
    return await admission.admit(tenant, reserved_usd=world["reserved"])


def _refused() -> float:
    return metric_value(admission.metrics.agent_credit_enforcement_refused_total)


def _degraded(path: str) -> float:
    return metric_value(admission.metrics.agent_cost_governance_degraded_total, path=path)


class TestWhoIsGatedAndWhatCountsAsCredit:
    async def test_a_tenant_with_no_wallet_is_never_gated_and_needs_no_hold_arithmetic(self, world):
        """Shipping the gate changes nothing for a tenant until a wallet is opened for it."""
        before = _refused()

        answer = await _admit(world)

        assert answer.status == "allowed" and not answer.refused
        assert _refused() == before and world["hold_reads"] == 0

    async def test_a_tenant_with_credits_is_served(self, world):
        world["wallet"] = _balance(100)

        assert (await _admit(world)).status == "allowed"

    async def test_a_tenant_with_a_wallet_and_nothing_in_it_is_refused(self, world):
        """Unlike a tenant with NO wallet: an account at zero is the prepaid customer who ran out."""
        world["wallet"] = _balance(0)
        before = _refused()

        answer = await _admit(world)

        assert answer.status == "exhausted" and answer.refused
        assert _refused() == before + 1
        assert (answer.available, answer.reserved) == (D("0"), D("0"))

    async def test_a_tenant_in_debt_is_refused_until_it_is_repaid(self, world):
        world["wallet"] = _balance(-25)

        answer = await _admit(world)

        assert answer.status == "exhausted" and answer.available == D("-25")

    async def test_the_smallest_positive_balance_is_enough(self, world):
        world["wallet"] = _balance("0.000001")

        assert (await _admit(world)).status == "allowed"

    async def test_the_wallet_read_is_scoped_to_the_tenant_asked_about(self, world):
        world["wallet"] = _balance(5)

        await _admit(world, tenant="globex")

        assert world["wallet_reads"] == ["globex"]


class TestHoldsAreInCredits:
    async def test_in_flight_work_is_converted_at_the_configured_rate(self, world):
        """One turn in flight holds MAX_COST_USD_PER_TURN dollars; at 1000 credits per dollar, $0.50 is 500 credits, so a tenant with 100
        cannot start a second concurrent turn that may spend 500."""
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.50

        answer = await _admit(world)

        assert answer.status == "exhausted" and answer.reserved == D("500.000000")

    async def test_a_small_hold_leaves_the_tenant_able_to_start(self, world):
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.05  # 50 credits

        assert (await _admit(world)).status == "allowed"

    async def test_the_markup_applies_to_holds_as_it_does_to_charges(self, world, monkeypatch):
        """A hold that ignored the markup would let a marked-up tenant start work it cannot pay for."""
        monkeypatch.setattr(admission, "MARKUP", D("1.5"))
        world["wallet"] = _balance(100)
        world["reserved_usd"] = 0.05  # 50 credits at cost, 75 with a 1.5x markup

        assert (await _admit(world)).status == "allowed"
        world["reserved_usd"] = 0.0667  # 66.7 credits at cost, 100.05 with the markup
        assert (await _admit(world)).status == "exhausted"

    async def test_a_balance_exactly_used_up_by_holds_is_not_positive_so_it_is_refused(self, world):
        world["wallet"] = _balance(50)
        world["reserved_usd"] = 0.05  # exactly 50 credits

        assert (await _admit(world)).status == "exhausted"

    async def test_the_holds_are_read_once_and_only_for_a_tenant_that_has_a_wallet(self, world):
        world["wallet"] = _balance(5)
        await _admit(world)
        world["wallet"] = None
        await _admit(world)

        assert world["hold_reads"] == 1


class TestEnforcementOffChangesNothing:
    """Without enforcement the wallet is not so much ignored as never reached (SC-005 of spec 010)."""

    async def test_a_tenant_with_no_credits_is_served_and_nothing_is_read(self, world, monkeypatch):
        monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", False)
        world["wallet"] = _balance(0)
        before = _refused()

        answer = await _admit(world)

        assert answer.status == "allowed"
        assert world["wallet_reads"] == [] and world["hold_reads"] == 0 and _refused() == before

    async def test_a_wallet_that_would_fail_to_read_cannot_affect_anything(self, world, monkeypatch):
        monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", False)
        world["wallet_error"] = ConnectionError("wallet down")
        before = _degraded("credit_read")

        assert (await _admit(world)).status == "allowed"
        assert _degraded("credit_read") == before

    async def test_enforcement_without_a_rate_is_a_loud_error_never_a_silent_no_gate(self, world, monkeypatch):
        """Settings refuse this at startup; this is for a caller that re-points the globals into a state they forbid. Quietly skipping the gate
        would serve a tenant the operator meant to stop."""
        monkeypatch.setattr(admission, "CREDITS_PER_USD", None)

        with pytest.raises(RuntimeError, match="CREDITS_PER_USD"):
            await _admit(world)


class TestWhenTheWalletCannotBeRead:
    async def test_open_serves_counts_it_and_marks_the_answer_unverified(self, world):
        world["wallet_error"] = ConnectionError("wallet unreachable")
        before = _degraded("credit_read")

        answer = await _admit(world)

        assert answer.status == "degraded" and not answer.refused
        assert _degraded("credit_read") == before + 1

    async def test_closed_refuses_as_unavailable_and_still_counts_it(self, world, monkeypatch):
        monkeypatch.setattr(admission, "CREDIT_CHECK_FAILURE_POLICY", "closed")
        world["wallet_error"] = ConnectionError("wallet unreachable")
        before = _degraded("credit_read")

        answer = await _admit(world)

        assert answer.status == "unavailable" and answer.refused
        assert _degraded("credit_read") == before + 1
        assert world["hold_reads"] == 0

    async def test_a_missing_wallet_table_is_the_same_counted_path(self, world):
        from psycopg import errors as pg_errors

        world["wallet_error"] = pg_errors.UndefinedTable('relation "credit_accounts" does not exist')
        before = _degraded("credit_read")

        assert (await _admit(world)).status == "degraded"
        assert _degraded("credit_read") == before + 1

    async def test_the_log_carries_the_error_class_and_never_its_text(self, world, caplog):
        world["wallet_error"] = ConnectionError("postgresql://user:hunter2@db/appdata")

        with caplog.at_level("WARNING", logger=admission.logger.name):
            await _admit(world)

        (record,) = [r for r in caplog.records if r.getMessage() == "credit_check_failed"]
        assert record.error_class == "ConnectionError" and "hunter2" not in str(record.__dict__)
