"""The billing port: the one seam between the agent app and the wallet (specs/011-billing-module-boundary, D18, D19).

`packages/billing/src/credit_billing/port.py` is the contract (`WalletPort`) and `app/billing_integration/port.py` is the module host code
imports. These tests prove what a seam is for, in four steps:

  1. the implementation that exists IS the contract, down to the signature, so a drift fails here and not in production;
  2. it adds nothing: each call reaches `credits` with exactly the arguments the host used to pass directly;
  3. a DIFFERENT implementation of the protocol, installed where the host looks, drives the charge, the gate and
     `GET /usage` while the real wallet is rigged to fail the test if it is touched, so the seam is real and complete;
  4. the failure behaviour a host depends on (a wallet fault never loses the usage event; an unreadable wallet follows
     `CREDIT_CHECK_FAILURE_POLICY`) is a property of the host's call sites, not of the in-process implementation.

What the wallet's SQL does is proven against a real Postgres elsewhere (tests/integration/test_credit_charge_real_postgres.py
runs the whole charge through this seam).
"""
import inspect
import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.agent import budget_holds, budgets, spend, usage_events
from app.api.routers import usage as usage_router
from app.billing_integration import port as billing_port
from app.core import metrics
from credit_billing import credits
from credit_billing import port as core_port
from tests.conftest import TEST_CTX, metric_value

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GATE = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1"), fail_policy="open")
CLOSED = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1"), fail_policy="closed")
OPERATIONS = ("record_usage_in", "account_balance")


def _shape(function) -> tuple:
    """Everything about a signature that a caller can depend on, except `self`."""
    signature = inspect.signature(function)
    return (
        [(p.name, p.kind, p.default, p.annotation) for name, p in signature.parameters.items() if name != "self"],
        signature.return_annotation,
    )


# --- 1. the implementation is the contract -------------------------------------------------------------------------

def test_the_contract_names_exactly_the_two_operations_the_host_uses():
    """A third operation arriving by accident would widen what every future implementation (a network client) must offer."""
    public = {name for name, member in vars(core_port.WalletPort).items() if not name.startswith("_") and callable(member)}

    assert public == set(OPERATIONS)


def test_the_in_process_wallet_is_an_implementation_of_the_contract():
    assert isinstance(core_port.InProcessWallet(), core_port.WalletPort)


@pytest.mark.parametrize("name", OPERATIONS)
def test_the_in_process_wallet_has_the_contracts_exact_signature(name):
    assert _shape(getattr(core_port.InProcessWallet, name)) == _shape(getattr(core_port.WalletPort, name))


@pytest.mark.parametrize("name", OPERATIONS)
def test_what_the_host_calls_has_the_contracts_exact_signature(name):
    assert _shape(getattr(billing_port, name)) == _shape(getattr(core_port.WalletPort, name))


def test_rating_is_the_same_function_on_both_sides_of_the_seam():
    for cost, rate, markup in [(D("0.0000004"), D("1000"), D("1")), (D("0.0123"), D("1000"), D("1.5")), (0.0123, D("1000"), D("1")), (D("0"), D("1"), D("1"))]:
        assert billing_port.credits_for_cost(cost, rate, markup) == credits.credits_for_cost(cost, rate, markup)
        assert core_port.credits_for_cost(cost, rate, markup) == credits.credits_for_cost(cost, rate, markup)


# --- 2. it adds nothing --------------------------------------------------------------------------------------------

async def test_record_usage_in_reaches_debit_in_with_exactly_the_arguments_the_host_used_to_pass(monkeypatch):
    seen = {}

    async def debit_in(conn, tenant, amount, **kwargs):
        seen.update(conn=conn, tenant=tenant, amount=amount, kwargs=kwargs)
        return credits.Applied("applied", "tx-1", D("10"))

    monkeypatch.setattr(credits, "debit_in", debit_in)
    pricing = credits.Pricing(cost_usd=D("0.01"), credits_per_usd=D("1000"), markup=D("1"))

    applied = await billing_port.record_usage_in("THE-CONNECTION", "acme", D("10"), event_id="ev-1", kind="chat", pricing=pricing)

    assert seen == {
        "conn": "THE-CONNECTION",
        "tenant": "acme",
        "amount": D("10"),
        "kwargs": {"idempotency_key": "ev-1", "usage_event_id": "ev-1", "reason": "model call (chat)", "pricing": pricing},
    }, "the event id is the idempotency key AND the lineage id, the reason names the call's kind, the pricing rides along"
    assert applied == credits.Applied("applied", "tx-1", D("10"))


async def test_account_balance_reaches_the_wallet_and_passes_a_missing_wallet_through_as_none(monkeypatch):
    reads = []
    balance = credits.Balance(available=D("3"), ledger=D("3"), debt=D("0"))

    async def account_balance(tenant):
        reads.append(tenant)
        return None if tenant == "nobody" else balance

    monkeypatch.setattr(credits, "account_balance", account_balance)

    assert await billing_port.account_balance("acme") == balance
    assert await billing_port.account_balance("nobody") is None, "no wallet is None, never a zero balance (spec D8)"
    assert reads == ["acme", "nobody"]


# --- 3. a different implementation drives the whole host path ------------------------------------------------------

class StubWallet:
    """Implements `WalletPort` and nothing else. Records what it was asked; can be told to fail."""

    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self.reads: list[str] = []
        self.balance: credits.Balance | None = None
        self.error: Exception | None = None

    async def record_usage_in(self, conn, tenant, amount, *, event_id, kind, pricing=None):
        self.writes.append((conn, tenant, amount, event_id, kind, pricing))
        if self.error:
            raise self.error
        return credits.Applied("applied")

    async def account_balance(self, tenant):
        self.reads.append(tenant)
        if self.error:
            raise self.error
        return self.balance


class Connection:
    """Just enough connection for `_charge`: a savepoint that records how it ended."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def transaction(self):
        events = self.events

        class Savepoint:
            async def __aenter__(self):
                events.append("savepoint")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                events.append("rolled back" if exc_type else "released")
                return False

        return Savepoint()


@pytest.fixture
def stub(monkeypatch):
    """The stub installed where the host looks, with the REAL wallet rigged to fail the test if anything reaches it."""
    def real_wallet_touched(*args, **kwargs):
        raise AssertionError("the real wallet was reached: the host did not go through the port")

    monkeypatch.setattr(credits, "debit_in", real_wallet_touched)
    monkeypatch.setattr(credits, "account_balance", real_wallet_touched)
    wallet = StubWallet()
    assert isinstance(wallet, core_port.WalletPort), "the stub must itself satisfy the contract"
    monkeypatch.setattr(billing_port, "wallet", wallet)
    return wallet


ROW = {
    "tenant": "acme", "credits": D("5"), "event_id": "ev-9", "kind": "chat",
    "cost_usd": D("0.005"), "credits_per_usd": D("1000"), "markup": D("1"),
}


def _degraded(path: str) -> float:
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


class TestTheChargeGoesThroughThePort:
    async def test_the_usage_events_charge_reaches_the_installed_wallet_inside_its_savepoint(self, stub):
        conn = Connection()

        await usage_events._charge(conn, dict(ROW))

        (written,) = stub.writes
        assert written == (
            conn, "acme", D("5"), "ev-9", "chat",
            credits.Pricing(cost_usd=D("0.005"), credits_per_usd=D("1000"), markup=D("1")),
        )
        assert conn.events == ["savepoint", "released"], "the debit runs on the event's own connection, inside a savepoint"

    async def test_a_wallet_that_fails_is_swallowed_counted_and_only_its_savepoint_is_undone(self, stub):
        stub.error = RuntimeError("the wallet is down")
        conn = Connection()
        before = _degraded("credit_debit")

        await usage_events._charge(conn, dict(ROW))  # must not raise: the meter outranks the charge

        assert conn.events == ["savepoint", "rolled back"]
        assert _degraded("credit_debit") == before + 1, "a failed debit is counted (alert CreditDebitFailing)"


    async def test_a_reused_event_id_with_a_different_request_is_swallowed_counted_and_named_in_the_log_and_the_event_is_kept(self, stub, caplog):
        """Spec 012: the wallet now refuses a replay whose request differs. The meter still outranks the charge, so the usage event is kept;
        the fault is counted like any other wallet fault (alert CreditDebitFailing) and the log says WHICH kind it was."""
        stub.error = billing_port.IdempotencyConflict("ev-9", "acme", {"amount": ("5.000000", "6.000000")})
        conn = Connection()
        before = _degraded("credit_debit")

        with caplog.at_level(logging.WARNING):
            await usage_events._charge(conn, dict(ROW))  # must not raise

        assert conn.events == ["savepoint", "rolled back"], "only the debit's savepoint is undone"
        assert _degraded("credit_debit") == before + 1
        failed = next(r for r in caplog.records if r.getMessage() == "credit_debit_failed")
        assert failed.error_class == "IdempotencyConflict"


def test_the_conflict_error_is_one_class_whichever_module_it_is_reached_through():
    assert billing_port.IdempotencyConflict is core_port.IdempotencyConflict is credits.IdempotencyConflict
    assert issubclass(billing_port.IdempotencyConflict, ValueError)


class TestTheGateGoesThroughThePort:
    @pytest.fixture(autouse=True)
    def no_other_reads(self, monkeypatch):
        async def nothing_in_flight(tenant):
            return 0.0

        monkeypatch.setattr(budget_holds, "in_flight_reservation", nothing_in_flight)

    async def _check(self, gate=GATE):
        return await budgets.check_allowance(TEST_CTX, limits=[], fail_policy="open", now=NOW, credit_gate=gate)

    async def test_a_tenant_with_credit_is_served_and_the_wallet_read_is_the_stubs(self, stub):
        stub.balance = credits.Balance(available=D("100"), ledger=D("100"), debt=D("0"))

        allowance = await self._check()

        assert allowance.status == "ok"
        assert stub.reads == [TEST_CTX["tenant"]], "only the caller's own tenant is read"

    async def test_a_tenant_with_a_wallet_and_nothing_in_it_is_refused(self, stub):
        stub.balance = credits.Balance(available=D("0"), ledger=D("0"), debt=D("0"))

        assert (await self._check()).refused

    async def test_a_tenant_with_no_wallet_is_never_gated(self, stub):
        stub.balance = None

        assert (await self._check()).status == "ok"

    async def test_an_unreadable_wallet_follows_the_failure_policy_the_deployment_chose(self, stub):
        stub.error = RuntimeError("the wallet is down")
        before = _degraded("credit_read")

        served = await self._check(GATE)  # open: serve the turn, counted
        refused = await self._check(CLOSED)  # closed: refuse it, counted

        assert served.status == "ok" and refused.status == "unavailable"
        assert _degraded("credit_read") == before + 2


class TestTheUsageEndpointGoesThroughThePort:
    async def test_it_reports_the_stubs_balance_for_the_callers_own_tenant(self, stub, monkeypatch):
        async def usage_summary(tenant, principal=None, since=None):
            return {"total_tokens": 0, "total_cost_usd": 0.0}

        monkeypatch.setattr(spend, "usage_summary", usage_summary)
        monkeypatch.setattr(usage_router, "MAX_COST_USD_PER_TENANT_PER_DAY", 20.0)
        monkeypatch.setattr(usage_router, "CREDITS_PER_USD", D("1000"))
        monkeypatch.setattr(usage_router, "CREDITS_ENFORCEMENT", True)
        stub.balance = credits.Balance(available=D("12.5"), ledger=D("12.5"), debt=D("0"))

        result = await usage_router.usage(ctx=TEST_CTX)

        assert result.credits.available == D("12.5") and result.credits.enforced is True
        assert stub.reads == [TEST_CTX["tenant"]]
