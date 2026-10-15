"""The billing port seen from the agent app: the one seam between the agent and billing (specs/011-billing-module-boundary, D18, D19; specs/013).

`packages/billing/src/credit_billing/port.py` is the contract (`BillingPort`) and `app/billing_integration/port.py` is the module host code
imports. The package's own tests prove its implementation is the contract and adds nothing (packages/billing/tests/test_port.py). These prove the
agent's side, in three steps:

  1. what the agent calls has the contract's exact signature, so a drift fails here and not in production;
  2. a DIFFERENT implementation of the protocol, installed where the host looks, drives the hand-off of a usage event, the credit gate and
     `GET /usage` while the real wallet is rigged to fail the test if it is touched, so the seam is real and complete;
  3. a fault behind the seam never costs the agent its own record.

What billing then DOES with a usage event (rating, the wallet, the export) is the package's: packages/billing/tests/. The whole path against a
real Postgres is tests/integration/test_billing_hand_off_real_postgres.py.
"""
import inspect
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.agent.cost import budget_holds, budgets, spend, usage_events
from app.api.routers import usage as usage_router
from app.billing_integration import port as billing_port
from app.core import metrics
from credit_billing import admission, credits, usage
from credit_billing import metrics as billing_metrics
from credit_billing import port as core_port
from tests.conftest import TEST_CTX, metric_value

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OPERATIONS = ("record_usage_in", "account_balance", "admit", "credit_summary")
_REAL_INSERT = usage_events._insert  # imported before any autouse fixture patches it


def _shape(function) -> tuple:
    """Everything about a signature that a caller can depend on, except `self`."""
    signature = inspect.signature(function)
    return (
        [(p.name, p.kind, p.default, p.annotation) for name, p in signature.parameters.items() if name != "self"],
        signature.return_annotation,
    )


# --- 1. the agent calls the contract ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", OPERATIONS)
def test_what_the_host_calls_has_the_contracts_exact_signature(name):
    assert _shape(getattr(billing_port, name)) == _shape(getattr(core_port.BillingPort, name))


def test_the_host_reaches_the_package_only_through_the_data_types_the_port_re_exports():
    for name in ("Usage", "Recorded", "Admission", "CreditSummary", "Balance"):
        assert getattr(billing_port, name) is getattr(core_port, name)


# --- 2. a different implementation drives the whole host path ------------------------------------------------------

class StubBilling:
    """Implements `BillingPort` and nothing else. Records what it was asked; can be told to fail."""

    def __init__(self) -> None:
        self.records: list[tuple] = []
        self.reads: list[str] = []
        self.admissions: list[tuple] = []
        self.answer = billing_port.Admission("allowed")
        self.summary: billing_port.CreditSummary | None = None
        self.error: Exception | None = None

    async def record_usage_in(self, conn, usage):
        self.records.append((conn, usage))
        if self.error:
            raise self.error
        return billing_port.Recorded("recorded")

    async def account_balance(self, tenant):
        self.reads.append(tenant)
        return None

    async def admit(self, tenant, *, reserved_usd):
        self.admissions.append((tenant, reserved_usd))
        if self.error:
            raise self.error
        return self.answer

    async def credit_summary(self, tenant):
        self.reads.append(tenant)
        if self.error:
            raise self.error
        return self.summary


class Connection:
    """Just enough connection for the agent's insert: the statement it ran and whether a row was written."""

    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.statements: list[tuple] = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return SimpleNamespace(rowcount=self.rowcount)


@pytest.fixture
def stub(monkeypatch):
    """The stub installed where the host looks, with the REAL package rigged to fail the test if anything reaches it."""

    def real_billing_touched(*args, **kwargs):
        raise AssertionError("the real billing package was reached: the host did not go through the port")

    monkeypatch.setattr(usage, "record_usage_in", real_billing_touched)
    monkeypatch.setattr(admission, "admit", real_billing_touched)
    monkeypatch.setattr(credits, "account_balance", real_billing_touched)
    billing = StubBilling()
    assert isinstance(billing, core_port.BillingPort), "the stub must itself satisfy the contract"
    monkeypatch.setattr(billing_port, "billing", billing)
    return billing


ROW = {
    "event_id": "ev-9", "tenant": "acme", "principal": "alice", "thread_id": "t", "kind": "chat", "model_alias": "chat",
    "resolved_model": "gpt-resolved", "input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 0, "total_tokens": 150,
    "cost_usd": 0.0123, "price_input_per_token": 0.0001, "price_output_per_token": 0.0002,
}


@pytest.fixture
def connection(monkeypatch):
    handed_out: list[Connection] = []
    config = SimpleNamespace(rowcount=1)

    @asynccontextmanager
    async def get_connection():
        conn = Connection(config.rowcount)
        handed_out.append(conn)
        yield conn

    monkeypatch.setattr(usage_events, "get_connection", get_connection)
    return SimpleNamespace(handed_out=handed_out, config=config)


class TestTheUsageEventIsHandedOverThroughThePort:
    async def test_a_new_event_hands_billing_plain_facts_on_the_connection_it_was_inserted_on(self, stub, connection):
        assert await _REAL_INSERT(dict(ROW)) is True

        (conn,) = connection.handed_out
        ((handed_conn, handed),) = stub.records
        assert handed_conn is conn, "the same connection is the same transaction: the two commit or roll back together"
        assert handed == billing_port.Usage(
            tenant="acme", event_id="ev-9", kind="chat", cost_usd=0.0123, model="gpt-resolved", tokens=150
        )
        assert [sql.split()[0] for sql, _ in conn.statements] == ["INSERT"], "the agent's own insert is the only statement it runs"

    async def test_the_model_is_the_resolved_one_and_falls_back_to_the_alias(self, stub, connection):
        await _REAL_INSERT(dict(ROW, resolved_model=None))

        assert stub.records[0][1].model == "chat"

    async def test_an_unpriced_call_is_handed_over_as_unknown_never_zero(self, stub, connection):
        await _REAL_INSERT(dict(ROW, cost_usd=None))

        assert stub.records[0][1].cost_usd is None

    async def test_a_duplicate_event_hands_billing_nothing(self, stub, connection):
        connection.config.rowcount = 0  # ON CONFLICT DO NOTHING: the event was already there

        assert await _REAL_INSERT(dict(ROW)) is False

        assert stub.records == []

    async def test_the_agents_insert_names_no_billing_column(self, stub, connection):
        await _REAL_INSERT(dict(ROW))

        sql = connection.handed_out[0].statements[0][0]
        assert "credits" not in sql and "markup" not in sql and "outbox" not in sql


class TestAFaultBehindThePortNeverCostsTheAgentItsOwnRecord:
    async def test_an_implementation_that_raises_is_swallowed_counted_and_the_event_still_stands(self, stub, connection, caplog):
        stub.error = RuntimeError("the billing service is down")
        before = metric_value(billing_metrics.agent_cost_governance_degraded_total, path="billing_event_write")
        write_before = metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write")

        with caplog.at_level(logging.WARNING):
            assert await _REAL_INSERT(dict(ROW)) is True, "the agent's event was written; only the hand-off failed"

        assert metric_value(billing_metrics.agent_cost_governance_degraded_total, path="billing_event_write") == before + 1
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write") == write_before, "this is not a lost agent event"
        failed = next(r for r in caplog.records if r.getMessage() == "billing_usage_hand_off_failed")
        assert failed.error_class == "RuntimeError"


class TestTheGateGoesThroughThePort:
    @pytest.fixture(autouse=True)
    def no_other_reads(self, monkeypatch):
        async def nothing_in_flight(tenant):
            return 0.0

        monkeypatch.setattr(budget_holds, "in_flight_reservation", nothing_in_flight)

    async def _check(self):
        return await budgets.check_allowance(TEST_CTX, limits=[], fail_policy="open", now=NOW)

    async def test_an_allowed_answer_serves_the_turn_and_only_the_callers_own_tenant_is_asked_about(self, stub):
        allowance = await self._check()

        assert allowance.status == "ok" and not allowance.degraded
        assert [tenant for tenant, _ in stub.admissions] == [TEST_CTX["tenant"]]

    async def test_an_exhausted_answer_refuses_with_the_figures_it_carried(self, stub):
        stub.answer = billing_port.Admission("exhausted", D("-3"), D("500"))

        allowance = await self._check()

        assert allowance.status == "insufficient_credits" and allowance.refused
        assert (allowance.available_credits, allowance.reserved_credits) == (D("-3"), D("500"))

    async def test_an_unavailable_answer_refuses_as_unavailable_which_blames_no_ones_balance(self, stub):
        stub.answer = billing_port.Admission("unavailable")

        allowance = await self._check()

        assert allowance.status == "unavailable" and allowance.refused
        assert budgets.refusal_envelope(allowance).code.value == "budget_check_unavailable"

    async def test_a_degraded_answer_serves_the_turn_and_marks_it_unverified(self, stub):
        stub.answer = billing_port.Admission("degraded")

        allowance = await self._check()

        assert allowance.status == "ok" and allowance.degraded is True

    async def test_the_holds_are_handed_over_lazily_so_a_gate_that_does_not_need_them_never_pays_for_the_read(self, stub, monkeypatch):
        reads = []

        async def in_flight(tenant):
            reads.append(tenant)
            return 0.25

        monkeypatch.setattr(budget_holds, "in_flight_reservation", in_flight)

        await self._check()
        assert reads == [], "the gate did not call the reader, so nothing was read"

        _, reserved_usd = stub.admissions[0]
        assert await reserved_usd() == 0.25 and reads == [TEST_CTX["tenant"]]


class TestTheUsageEndpointGoesThroughThePort:
    async def test_it_reports_the_stubs_balance_for_the_callers_own_tenant(self, stub, monkeypatch):
        async def usage_summary(tenant, principal=None, since=None):
            return {"total_tokens": 0, "total_cost_usd": 0.0}

        monkeypatch.setattr(spend, "usage_summary", usage_summary)
        monkeypatch.setattr(usage_router, "MAX_COST_USD_PER_TENANT_PER_DAY", 20.0)
        stub.summary = billing_port.CreditSummary(credits.Balance(D("12.5"), D("12.5"), D("0")), enforced=True)

        result = await usage_router.usage(ctx=TEST_CTX)

        assert result.credits.available == D("12.5") and result.credits.enforced is True
        assert stub.reads == [TEST_CTX["tenant"]]
