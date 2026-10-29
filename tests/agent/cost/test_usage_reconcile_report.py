"""One reconciliation pass end to end (app/agent/cost/usage_reconcile.py::reconcile), with the database reads replaced by figures so every
disagreement is chosen by the test; the same pass against a real Postgres is tests/integration/test_usage_reconcile_real_postgres.py.

The headline (specs/010 T026): delete ONE usage event and the report names the tenant, the day and the amount. (The wallet half, was every
usage worth credits charged?, is the billing package's: packages/billing/tests/test_reconcile.py.)"""
import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

import httpx
import pytest

from app.adapters.litellm.spend_logs import HttpGatewaySpendLog
from app.agent.cost import usage_reconcile as reconcile
from app.agent.cost.usage_reconcile import Report, Tolerance
from app.core import metrics
from app.core.gateway import end_user_id
from app.ports import cost as cost_ports
from app.ports.cost import EventsByTenantDay, Window
from tests.agent.fake_gateway import FakeGateway, spend_row
from tests.conftest import metric_value
from tests.fakes.cost import InMemoryUsageEventStore

DAY = date(2026, 10, 6)
WINDOW = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 18, 0, tzinfo=UTC))
NOON = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
STRICT = Tolerance(usd=D("0.001"), pct=D("0"))


class _Sources(InMemoryUsageEventStore):
    """The records, as a test sets them: events by (tenant, day) and the tenants the database knows."""

    def __init__(self):
        super().__init__()
        self.state = {"events": {}, "unpriced": {}, "tenants": {"acme"}}

    async def by_tenant_day(self, window):
        s = self.state
        return EventsByTenantDay(dict(s["events"]), dict(s["unpriced"]), len(s["events"]))

    async def known_tenants(self):
        return set(self.state["tenants"])


@pytest.fixture
def sources():
    store = _Sources()
    with cost_ports.usage_events.overriding(store):
        yield store.state


def gateway_saw(*spend: float, end_user: str | None = None) -> FakeGateway:
    end_user = end_user or end_user_id("acme", "alice")
    return FakeGateway([spend_row(end_user, NOON, amount, f"req-{i}") for i, amount in enumerate(spend)])


async def run(gateway: FakeGateway | None, **kwargs) -> Report:
    kwargs.setdefault("tolerance", STRICT)
    if gateway is None:
        return await reconcile.reconcile(WINDOW, **kwargs)
    async with gateway.client() as client:
        return await reconcile.reconcile(WINDOW, gateway=HttpGatewaySpendLog(client), **kwargs)


class TestTheRecordsAgree:
    async def test_matching_records_are_ok(self, sources):
        sources["events"] = {("acme", DAY): D("1.500000")}

        report = await run(gateway_saw(1.0, 0.5))

        assert report.outcome == "ok" and report.findings == []
        assert report.gateway_rows == 2 and report.events == 1
        assert "OK" in reconcile.render(report)

    async def test_differences_inside_the_tolerance_are_not_drift(self, sources):
        sources["events"] = {("acme", DAY): D("100")}

        report = await run(gateway_saw(100.9), tolerance=Tolerance(usd=D("0.01"), pct=D("1")))

        assert report.outcome == "ok"


class TestADeletedEvent:
    async def test_the_report_names_the_tenant_the_day_and_the_amount(self, sources):
        # The meter recorded 1.0 and 0.5; the 0.5 event is deleted. The gateway and the ledger still hold both.
        sources["events"] = {("acme", DAY): D("1.0")}

        report = await run(gateway_saw(1.0, 0.5))

        gateway = [f for f in report.findings if f.kind == "gateway"]
        assert [(f.tenant, f.day, f.drift) for f in gateway] == [("acme", DAY, D("0.5"))]
        assert report.outcome == "drift" and report.max_drift_usd == D("0.5")
        text = reconcile.render(report)
        assert "acme" in text and "2026-10-06" in text and "drift +$0.500000" in text
        assert "a call whose event was never written" in text

    async def test_a_tenant_whose_every_event_is_gone_is_still_named_from_the_gateway_alone(self, sources):
        sources["tenants"] = {"acme"}  # a tenant the database still knows by its earlier events

        report = await run(gateway_saw(2.0))

        assert [(f.tenant, f.drift) for f in report.findings] == [("acme", D("2.0"))]

    async def test_spend_for_a_tenant_the_database_has_never_heard_of_is_named_by_its_gateway_id(self, sources):
        stranger = "tenant_0123456789abcdef"

        report = await run(gateway_saw(3.0, end_user=stranger))

        (finding,) = report.findings
        assert stranger in finding.tenant and finding.drift == D("3.0")


class TestEveryComparisonStandsOnItsOwn:
    async def test_events_above_the_gateway_is_reported_with_its_own_sign_and_counts_by_size(self, sources):
        sources["events"] = {("acme", DAY): D("2.0")}

        report = await run(gateway_saw(1.0))

        (finding,) = report.findings
        assert finding.kind == "gateway" and finding.drift == D("-1.0")
        assert report.max_drift_usd == D("1.0")  # the size of the gap, whichever side is larger: a signed maximum would read it as zero

    async def test_an_unpriced_event_is_named_beside_the_gateway_gap_it_explains(self, sources):
        sources["events"] = {("acme", DAY): D("0")}
        sources["unpriced"] = {("acme", DAY): 3}

        report = await run(gateway_saw(1.0))

        assert "3 unpriced event(s)" in report.findings[0].note

    async def test_spend_the_gateway_cannot_attribute_is_reported_and_is_never_drift(self, sources):
        sources["events"] = {("acme", DAY): D("1.0")}
        gateway = FakeGateway([spend_row(end_user_id("acme", "alice"), NOON, 1.0, "a"), spend_row(None, NOON, 0.4, "embedding-1")])

        report = await run(gateway)

        assert report.outcome == "ok" and report.unattributed_usd == D("0.4")
        assert "0.4" in reconcile.render(report) and "not drift" in reconcile.render(report)


class TestWhatItCannotCompareIsSaidNotSkippedSilently:
    async def test_an_incomplete_gateway_read_is_never_compared_and_is_its_own_outcome(self, sources):
        sources["events"] = {("acme", DAY): D("1.0")}
        many = FakeGateway([spend_row(end_user_id("acme", "alice"), NOON, 0.0, f"r{i}") for i in range(2500)])

        report = await run(many, max_pages=1)

        assert report.outcome == "incomplete" and report.incomplete
        assert report.findings == []  # a short sum is not "acme is under-metered"
        assert any("gateway: not compared, the read is incomplete" in s for s in report.skipped)

    async def test_no_gateway_is_a_stated_choice_not_an_incomplete_pass(self, sources):
        sources["events"] = {("acme", DAY): D("1.0")}

        report = await run(None)

        assert report.outcome == "ok" and report.gateway_rows is None
        assert "gateway: not compared (not requested)" in report.skipped

    async def test_an_empty_window_reads_nothing_and_says_why(self):
        class Never(InMemoryUsageEventStore):
            async def by_tenant_day(self, window):
                raise AssertionError("an empty window must not touch the database")

            async def known_tenants(self):
                raise AssertionError("an empty window must not touch the database")

        with cost_ports.usage_events.overriding(Never()):
            report = await reconcile.reconcile(Window(WINDOW.end, WINDOW.start))

        assert report.skipped and "empty" in report.skipped[0] and report.findings == []

    async def test_a_gateway_that_refuses_the_key_fails_the_pass_instead_of_reading_as_all_clear(self, sources):
        gateway = FakeGateway([], key="the-real-key")

        async with gateway.client() as client:
            client.headers["Authorization"] = "Bearer someone-else"
            with pytest.raises(httpx.HTTPStatusError):
                await reconcile.reconcile(WINDOW, gateway=HttpGatewaySpendLog(client), tolerance=STRICT)


class TestWhatItPublishes:
    def test_the_gauge_carries_the_largest_drift_and_returns_to_zero_when_it_is_fixed(self):
        drifted = Report(WINDOW, STRICT, findings=[reconcile.Finding("gateway", "acme", DAY, D("0"), D("0.5"))])

        reconcile.publish_gauges(drifted)
        assert metric_value(metrics.agent_usage_reconcile_max_drift_usd) == 0.5

        reconcile.publish_gauges(Report(WINDOW, STRICT))
        assert metric_value(metrics.agent_usage_reconcile_max_drift_usd) == 0

    def test_each_outcome_is_counted_under_its_own_label(self):
        before = {o: metric_value(metrics.agent_usage_reconcile_total, outcome=o) for o in ("ok", "drift", "incomplete", "failed")}

        for outcome in ("ok", "drift", "incomplete", "failed", "failed"):
            reconcile.record_outcome(outcome)

        assert {o: metric_value(metrics.agent_usage_reconcile_total, outcome=o) - before[o] for o in before} == {
            "ok": 1, "drift": 1, "incomplete": 1, "failed": 2,
        }


class TestTheReportIsSafeToPrint:
    def test_a_tenant_name_or_note_with_terminal_escapes_is_shown_inert(self):
        hostile = "acme\x1b[2J\rforged"
        report = Report(WINDOW, STRICT, findings=[reconcile.Finding("gateway", hostile, DAY, D("0"), D("1"), note="n\x07ote")])

        text = reconcile.render(report)

        assert "\x1b" not in text and "\r" not in text and "\x07" not in text
        assert "acme\\x1b[2J\\rforged" in text and "n\\x07ote" in text


class TestTheReport:
    def test_json_carries_every_figure_as_an_exact_string(self):
        report = Report(
            WINDOW, Tolerance(D("0.01"), D("1")), tenants=2, events=3, gateway_rows=4,
            findings=[reconcile.Finding("gateway", "acme", DAY, D("1.000000000001"), D("2"), note="n")],
        )

        data = json.loads(json.dumps(report.to_dict()))

        assert data["outcome"] == "drift" and data["max_drift_usd"] == "0.999999999999"
        assert data["findings"][0] | {"meaning": ""} == {
            "kind": "gateway", "tenant": "acme", "day": "2026-10-06", "expected": "1.000000000001", "actual": "2",
            "drift": "0.999999999999", "note": "n", "meaning": "",
        }

    def test_the_text_report_states_the_window_and_the_tolerance_it_judged_by(self):
        text = reconcile.render(Report(WINDOW, Tolerance(D("0.01"), D("1"))))

        assert "2026-10-06 00:00 to 2026-10-06 18:00 UTC" in text
        assert "the larger of $0.01 and 1%" in text
