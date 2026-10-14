"""The wallet reconciliation (credit_billing/reconcile.py, specs/010 T026, moved here by specs/013): with the database reads replaced by figures so
every disagreement is chosen by the test. The same pass against a real Postgres is tests/integration/test_reconcile_real_postgres.py.

The headline: leave ONE usage event uncharged and the report names the tenant, the day and the amount.
"""
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from psycopg import errors as pg_errors

from credit_billing import metrics, reconcile
from credit_billing.reconcile import Finding, Report, Window
from tests.conftest import metric_value

DAY = date(2026, 10, 6)
WINDOW = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 18, 0, tzinfo=UTC))


@pytest.fixture
def sources(monkeypatch):
    """The wallet and the events, as a test sets them."""
    state = {"events": (0, 0), "uncharged": [], "wallet": (D("0"), D("0"))}

    async def events_in(window):
        return state["events"]

    async def uncharged(window):
        return list(state["uncharged"])

    async def wallet():
        return state["wallet"]

    monkeypatch.setattr(reconcile, "events_in", events_in)
    monkeypatch.setattr(reconcile, "uncharged_by_tenant_day", uncharged)
    monkeypatch.setattr(reconcile, "wallet_totals", wallet)
    return state


class TestEveryUsageWasCharged:
    async def test_nothing_uncharged_is_ok_and_the_report_says_what_was_read(self, sources):
        sources["events"] = (12, 3)
        sources["wallet"] = (D("1250.5"), D("40"))

        report = await reconcile.reconcile(WINDOW)

        assert report.outcome == "ok" and report.findings == [] and (report.events, report.tenants) == (12, 3)
        text = reconcile.render(report)
        assert "OK: every usage worth credits was charged." in text and "12 usage event(s)" in text
        assert "1250.5 usable, 40 owed" in text


class TestAnUnchargedEvent:
    async def test_the_report_names_the_tenant_the_day_and_the_amount(self, sources):
        sources["uncharged"] = [Finding("acme", DAY, 2, D("340"), D("0.340000"))]

        report = await reconcile.reconcile(WINDOW)

        assert report.outcome == "drift" and report.max_drift_usd == D("0.340000")
        text = reconcile.render(report)
        assert "acme" in text and "2026-10-06" in text and "2 event(s) worth 340 credits" in text and "$0.340000" in text
        assert "CreditDebitFailing" in text, "it says what to look at next"

    async def test_the_largest_figure_is_the_gauge_whichever_tenant_it_is(self, sources):
        sources["uncharged"] = [Finding("acme", DAY, 1, D("1"), D("0.01")), Finding("globex", DAY, 1, D("9"), D("0.09"))]

        assert (await reconcile.reconcile(WINDOW)).max_drift_usd == D("0.09")

    async def test_a_cost_smaller_than_a_millionth_of_a_dollar_never_reads_as_zero(self):
        assert reconcile.usd(D("0.0000004")) == "0.0000004"
        assert reconcile.usd(D("0")) == "0.000000"


class TestWhatItCannotDoIsSaidNotSkippedSilently:
    @pytest.mark.parametrize("error", [pg_errors.UndefinedTable, pg_errors.UndefinedColumn])
    async def test_missing_tables_fail_the_pass_instead_of_reading_as_all_clear(self, sources, monkeypatch, error):
        async def missing(window):
            raise error("relation or column does not exist")

        monkeypatch.setattr(reconcile, "uncharged_by_tenant_day", missing)

        with pytest.raises(error):
            await reconcile.reconcile(WINDOW)

    async def test_an_empty_window_reads_nothing_and_says_why(self, monkeypatch):
        async def never(*args, **kwargs):
            raise AssertionError("an empty window must not touch the database")

        for name in ("events_in", "uncharged_by_tenant_day", "wallet_totals"):
            monkeypatch.setattr(reconcile, name, never)

        report = await reconcile.reconcile(Window(WINDOW.end, WINDOW.start))

        assert report.skipped and "empty" in report.skipped[0] and report.findings == []


class TestTheWindow:
    def test_it_is_whole_utc_days_back_to_midnight_up_to_the_settle_time_at_whole_seconds(self):
        now = datetime(2026, 10, 6, 12, 30, 45, 999, tzinfo=UTC)

        window = reconcile.default_window(now, lookback_days=2, settle_seconds=900)

        assert window.start == datetime(2026, 10, 5, tzinfo=UTC) and window.end == datetime(2026, 10, 6, 12, 15, 45, tzinfo=UTC)

    def test_a_settle_time_that_reaches_back_past_the_start_makes_an_empty_window(self):
        now = datetime(2026, 10, 6, 0, 5, tzinfo=UTC)

        assert reconcile.default_window(now, lookback_days=1, settle_seconds=900).empty is True


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list[tuple] = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        rows = self.rows
        return SimpleNamespace(fetchall=_async(lambda: rows), fetchone=_async(lambda: rows[0] if rows else None))


def _async(fn):
    async def call():
        return fn()

    return call


@pytest.fixture
def database(monkeypatch):
    def install(rows):
        conn = FakeConnection(rows)

        @asynccontextmanager
        async def get_connection():
            yield conn

        monkeypatch.setattr(reconcile, "get_connection", get_connection)
        return conn

    return install


class TestTheStatementThatFindsThem:
    """Statement shape only: that the anti-join really finds an uncharged event is proven against a real Postgres."""

    async def test_it_asks_for_events_worth_credits_for_a_tenant_that_had_a_wallet_with_no_debit_keyed_by_the_event(self, database):
        conn = database([("acme", DAY, D("0.34"), 2, D("340"))])

        findings = await reconcile.uncharged_by_tenant_day(WINDOW)

        (sql, params), = conn.statements
        assert "FROM billing_usage_events e JOIN credit_accounts a ON a.tenant = e.tenant" in sql
        assert "e.credits > 0" in sql and "e.recorded_at >= a.created_at" in sql
        assert "t.tenant = e.tenant AND t.idempotency_key = e.event_id" in sql, "the debit's key IS the event id, per tenant"
        assert params == (WINDOW.start, WINDOW.end)
        assert findings == [Finding("acme", DAY, 2, D("340"), D("0.34"))]

    async def test_the_largest_comes_first_and_ties_are_ordered_by_tenant_and_day(self, database):
        database([("b", DAY, D("1"), 1, D("1")), ("a", DAY, D("1"), 1, D("1")), ("c", DAY, D("5"), 1, D("5"))])

        assert [f.tenant for f in await reconcile.uncharged_by_tenant_day(WINDOW)] == ["c", "a", "b"]

    async def test_wallet_totals_can_be_scoped_to_one_tenant(self, database):
        conn = database([(D("5"), D("1"))])

        assert await reconcile.wallet_totals("acme") == (D("5"), D("1"))

        assert conn.statements[0][1] == {"tenant": "acme"} and "%(tenant)s::text IS NULL OR tenant = %(tenant)s" in conn.statements[0][0]


class SpyGauge:
    """Records every `.labels(state=...).set(value)` and `.set(value)`. Read through a spy and not through the SDK on purpose: whether a collected
    synchronous gauge is cleared or re-exported is an SDK release's decision (1.44 clears it, 1.45 keeps it, found by the package's own CI job
    installing the newest allowed SDK), and what these tests are about is what the code SET."""

    def __init__(self):
        self.sets: dict[str, float] = {}

    def labels(self, **labels):
        spy = self

        class Bound:
            def set(self, value):
                spy.sets[labels["state"]] = value

        return Bound()


class TestWhatItPublishes:
    def test_the_gauge_carries_the_largest_figure_and_returns_to_zero_when_it_is_fixed(self):
        reconcile.publish_gauges(Report(WINDOW, findings=[Finding("acme", DAY, 1, D("500"), D("0.5"))]))
        assert metric_value(metrics.agent_credit_reconcile_max_drift_usd) == 0.5

        reconcile.publish_gauges(Report(WINDOW))
        assert metric_value(metrics.agent_credit_reconcile_max_drift_usd) == 0

    def test_the_outstanding_credits_are_published_only_when_the_wallet_was_read(self, monkeypatch):
        spy = SpyGauge()
        monkeypatch.setattr(metrics, "agent_credit_outstanding", spy)

        reconcile.publish_gauges(Report(WINDOW, outstanding_live=D("1250.5"), outstanding_debt=D("40")))

        assert spy.sets == {"available": 1250.5, "debt": 40}

    def test_a_pass_that_could_not_read_the_wallet_leaves_the_balance_gauges_alone(self, monkeypatch):
        spy = SpyGauge()
        monkeypatch.setattr(metrics, "agent_credit_outstanding", spy)

        reconcile.publish_gauges(Report(WINDOW))

        assert spy.sets == {}

    def test_each_outcome_is_counted_under_its_own_label(self):
        before = {o: metric_value(metrics.agent_credit_reconcile_total, outcome=o) for o in reconcile.OUTCOMES}

        for outcome in ("ok", "drift", "failed", "failed"):
            reconcile.record_outcome(outcome)

        assert {o: metric_value(metrics.agent_credit_reconcile_total, outcome=o) - before[o] for o in before} == {"ok": 1, "drift": 1, "failed": 2}

    def test_priming_creates_every_outcome_series_at_zero_so_the_first_failure_is_visible_to_increase(self):
        from tests.conftest import has_series

        reconcile.prime_outcomes()

        assert all(has_series(metrics.agent_credit_reconcile_total, outcome=o) for o in reconcile.OUTCOMES)


class TestTheReport:
    def test_a_tenant_name_with_terminal_escapes_is_shown_inert(self):
        report = Report(WINDOW, findings=[Finding("acme\x1b[2J\rforged", DAY, 1, D("1"), D("0.001"))])

        text = reconcile.render(report)

        assert "\x1b" not in text and "\r" not in text and "acme\\x1b[2J\\rforged" in text

    def test_json_carries_every_figure_as_an_exact_string(self):
        report = Report(
            WINDOW, tenants=2, events=3, outstanding_live=D("1.5"), outstanding_debt=D("0"),
            findings=[Finding("acme", DAY, 2, D("340.000001"), D("0.340000000001"))],
        )

        data = json.loads(json.dumps(report.to_dict()))

        assert data["outcome"] == "drift" and data["max_drift_usd"] == "0.340000000001" and data["outstanding"] == {"available": "1.5", "debt": "0"}
        assert data["findings"][0] | {"meaning": ""} == {
            "tenant": "acme", "day": "2026-10-06", "events": 2, "credits": "340.000001", "usd": "0.340000000001", "meaning": "",
        }

    def test_the_text_report_states_the_window_it_judged(self):
        assert "2026-10-06 00:00 to 2026-10-06 18:00 UTC" in reconcile.render(Report(WINDOW))
