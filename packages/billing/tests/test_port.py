"""The billing port: the contract a host application meets the package through (credit_billing/port.py; specs/011 D12, D18, D19; specs/013).

These tests prove what a seam is for, in two steps (the host's own side of it, a stub implementation driving the agent app's whole path, is
tests/billing/test_port.py in the agent app's repository):

  1. the implementation that exists IS the contract, down to the signature, so a drift fails here and not in production;
  2. it adds nothing: each call reaches the module that does the work with exactly the arguments the host passed.

What the work does against a real database is proven by the integration tier.
"""
import inspect
from decimal import Decimal

import pytest

from credit_billing import admission, credits, usage
from credit_billing import port as core_port
from credit_billing.usage import Recorded, Usage

D = Decimal
OPERATIONS = ("record_usage_in", "account_balance", "admit", "credit_summary")


def _shape(function) -> tuple:
    """Everything about a signature that a caller can depend on, except `self`."""
    signature = inspect.signature(function)
    return (
        [(p.name, p.kind, p.default, p.annotation) for name, p in signature.parameters.items() if name != "self"],
        signature.return_annotation,
    )


# --- 1. the implementation is the contract -------------------------------------------------------------------------

def test_the_contract_names_exactly_the_four_operations_a_host_needs():
    """A fifth operation arriving by accident would widen what every future implementation (a network client) must offer."""
    public = {name for name, member in vars(core_port.BillingPort).items() if not name.startswith("_") and callable(member)}

    assert public == set(OPERATIONS)


def test_the_in_process_implementation_is_an_implementation_of_the_contract():
    assert isinstance(core_port.InProcessBilling(), core_port.BillingPort)


@pytest.mark.parametrize("name", OPERATIONS)
def test_the_in_process_implementation_has_the_contracts_exact_signature(name):
    assert _shape(getattr(core_port.InProcessBilling, name)) == _shape(getattr(core_port.BillingPort, name))


def test_rating_is_the_same_function_on_both_sides_of_the_seam():
    for cost, rate, markup in [(D("0.0000004"), D("1000"), D("1")), (D("0.0123"), D("1000"), D("1.5")), (0.0123, D("1000"), D("1")), (D("0"), D("1"), D("1"))]:
        assert core_port.credits_for_cost(cost, rate, markup) == credits.credits_for_cost(cost, rate, markup)


def test_the_conflict_error_is_one_class_whichever_module_it_is_reached_through():
    assert core_port.IdempotencyConflict is credits.IdempotencyConflict
    assert issubclass(core_port.IdempotencyConflict, ValueError)


# --- 2. it adds nothing --------------------------------------------------------------------------------------------

async def test_record_usage_in_reaches_the_recorder_with_the_connection_and_the_usage_unchanged(monkeypatch):
    seen = {}

    async def record_usage_in(conn, event):
        seen.update(conn=conn, event=event)
        return Recorded("recorded", D("10"))

    monkeypatch.setattr(usage, "record_usage_in", record_usage_in)  # resolved at call time
    event = Usage("acme", "ev-1", "chat", 0.01, "gpt", 10)

    recorded = await core_port.InProcessBilling().record_usage_in("THE-CONNECTION", event)

    assert seen == {"conn": "THE-CONNECTION", "event": event} and recorded == Recorded("recorded", D("10"))


async def test_admit_reaches_the_gate_with_the_tenant_and_the_hold_reader_unchanged(monkeypatch):
    seen = {}

    async def reserved():
        return 0.5

    async def admit(tenant, *, reserved_usd):
        seen.update(tenant=tenant, reserved=reserved_usd)
        return admission.Admission("exhausted", D("1"), D("2"))

    monkeypatch.setattr(admission, "admit", admit)

    answer = await core_port.InProcessBilling().admit("acme", reserved_usd=reserved)

    assert seen == {"tenant": "acme", "reserved": reserved} and answer == admission.Admission("exhausted", D("1"), D("2"))


async def test_account_balance_reaches_the_wallet_and_passes_a_missing_wallet_through_as_none(monkeypatch):
    reads = []
    balance = credits.Balance(available=D("3"), ledger=D("3"), debt=D("0"))

    async def account_balance(tenant):
        reads.append(tenant)
        return None if tenant == "nobody" else balance

    monkeypatch.setattr(credits, "account_balance", account_balance)
    billing = core_port.InProcessBilling()

    assert await billing.account_balance("acme") == balance
    assert await billing.account_balance("nobody") is None, "no wallet is None, never a zero balance"
    assert reads == ["acme", "nobody"]


class TestTheCreditSummary:
    @pytest.fixture
    def wallet(self, monkeypatch):
        state = {"balance": credits.Balance(D("12.5"), D("12.5"), D("0")), "reads": []}

        async def account_balance(tenant):
            state["reads"].append(tenant)
            return state["balance"]

        monkeypatch.setattr(credits, "account_balance", account_balance)
        return state

    async def test_with_credits_off_it_is_none_and_the_wallet_is_never_read(self, wallet, monkeypatch):
        """A deployment that never set a rate must not read a table it may not have."""
        monkeypatch.setattr(core_port, "CREDITS_PER_USD", None)

        assert await core_port.InProcessBilling().credit_summary("acme") is None
        assert wallet["reads"] == []

    async def test_a_tenant_with_no_wallet_is_none_not_zero(self, wallet, monkeypatch):
        monkeypatch.setattr(core_port, "CREDITS_PER_USD", D("1000"))
        wallet["balance"] = None

        assert await core_port.InProcessBilling().credit_summary("acme") is None

    @pytest.mark.parametrize("enforced", [True, False])
    async def test_a_tenant_with_a_wallet_gets_its_balance_and_whether_the_gate_enforces(self, wallet, monkeypatch, enforced):
        monkeypatch.setattr(core_port, "CREDITS_PER_USD", D("1000"))
        monkeypatch.setattr(core_port, "CREDITS_ENFORCEMENT", enforced)

        summary = await core_port.InProcessBilling().credit_summary("acme")

        assert summary == core_port.CreditSummary(credits.Balance(D("12.5"), D("12.5"), D("0")), enforced)
        assert wallet["reads"] == ["acme"]

    async def test_an_unreadable_wallet_raises_rather_than_reading_as_no_wallet(self, wallet, monkeypatch):
        monkeypatch.setattr(core_port, "CREDITS_PER_USD", D("1000"))

        async def broken(tenant):
            raise ConnectionError("wallet unreachable")

        monkeypatch.setattr(credits, "account_balance", broken)

        with pytest.raises(ConnectionError):
            await core_port.InProcessBilling().credit_summary("acme")
