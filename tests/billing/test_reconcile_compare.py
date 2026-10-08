"""The pure half of the reconciliation (app/billing/reconcile.py): who is allowed to differ by how much, which window is read, how a
one-way gateway id becomes a tenant, and what counts as drift. No database and no gateway: the reading of each is in
test_reconcile_gateway.py and tests/integration/test_reconcile_real_postgres.py."""
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.agent.gateway import end_user_id
from app.billing.reconcile import (
    Finding,
    Tolerance,
    Window,
    attribute_gateway,
    compare,
    default_window,
)

DAY = date(2026, 10, 6)
NEXT = date(2026, 10, 7)


class TestTolerance:
    def test_a_difference_is_drift_only_above_the_larger_of_the_two_allowances(self):
        tolerance = Tolerance(usd=D("0.01"), pct=D("1"))

        assert tolerance.allows(D("0.50"), D("0.51"))  # exactly the fixed allowance
        assert not tolerance.allows(D("0.50"), D("0.5101"))
        assert tolerance.allows(D("1000"), D("1009.99"))  # 1% of the larger figure is $10.10
        assert not tolerance.allows(D("1000"), D("1011"))

    def test_the_percentage_is_of_the_larger_figure_so_the_direction_does_not_matter(self):
        tolerance = Tolerance(usd=D("0"), pct=D("1"))

        assert tolerance.allows(D("100"), D("101")) == tolerance.allows(D("101"), D("100"))

    def test_zero_tolerance_allows_only_equal_figures(self):
        assert Tolerance(D("0"), D("0")).allows(D("1.000001"), D("1.000001"))
        assert not Tolerance(D("0"), D("0")).allows(D("1.000001"), D("1.000002"))


class TestTheWindow:
    def test_it_reaches_back_whole_utc_days_and_stops_before_the_newest_traffic(self):
        now = datetime(2026, 10, 7, 13, 45, 12, 999999, tzinfo=UTC)

        window = default_window(now, lookback_days=2, settle_seconds=900)

        assert window.start == datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
        assert window.end == datetime(2026, 10, 7, 13, 30, 12, tzinfo=UTC)  # whole seconds, the gateway's own precision

    def test_one_day_of_lookback_is_today_only(self):
        window = default_window(datetime(2026, 10, 7, 13, 0, tzinfo=UTC), lookback_days=1, settle_seconds=0)

        assert window.start == datetime(2026, 10, 7, 0, 0, tzinfo=UTC)

    def test_a_clock_in_another_zone_is_read_as_the_utc_instant_it_is(self):
        plus_nine = timezone(timedelta(hours=9))

        window = default_window(datetime(2026, 10, 7, 5, 0, tzinfo=plus_nine), lookback_days=1, settle_seconds=0)

        assert window.start == datetime(2026, 10, 6, 0, 0, tzinfo=UTC)  # 05:00+09:00 is 20:00 UTC the day before

    def test_just_after_midnight_the_settle_period_can_empty_the_window(self):
        window = default_window(datetime(2026, 10, 7, 0, 5, tzinfo=UTC), lookback_days=1, settle_seconds=900)

        assert window.empty


class TestCompare:
    TOLERANCE = Tolerance(usd=D("0.01"), pct=D("1"))

    def test_agreeing_records_report_nothing(self):
        events = {("acme", DAY): D("2.500000"), ("globex", DAY): D("0.003")}
        other = {("acme", DAY): D("2.500001"), ("globex", DAY): D("0.004")}

        assert compare("gateway", events, other, self.TOLERANCE) == []

    def test_a_tenant_day_present_on_one_side_only_is_a_difference_from_zero(self):
        # This is what a DELETED EVENT looks like: the gateway still saw the spend, the meter has nothing for that day.
        findings = compare("gateway", {}, {("acme", DAY): D("0.765500")}, self.TOLERANCE)

        assert findings == [Finding("gateway", "acme", DAY, D("0"), D("0.765500"))]
        assert findings[0].drift == D("0.765500")

    def test_the_other_direction_is_reported_with_its_own_sign(self):
        (finding,) = compare("gateway", {("acme", DAY): D("3")}, {("acme", DAY): D("2")}, self.TOLERANCE)

        assert finding.drift == D("-1")
        assert "events with no call behind them" in finding.meaning

    def test_findings_are_largest_first_then_by_tenant_and_day(self):
        events = {("a", DAY): D("0"), ("b", DAY): D("0"), ("b", NEXT): D("0")}
        other = {("a", DAY): D("1"), ("b", DAY): D("5"), ("b", NEXT): D("1")}

        findings = compare("gateway", events, other, self.TOLERANCE)

        assert [(f.tenant, f.day) for f in findings] == [("b", DAY), ("a", DAY), ("b", NEXT)]

    def test_a_note_for_the_tenant_day_travels_with_its_finding(self):
        (finding,) = compare("gateway", {}, {("acme", DAY): D("1")}, self.TOLERANCE, {("acme", DAY): "2 unpriced event(s)"})

        assert finding.note == "2 unpriced event(s)"

    def test_two_tenants_with_the_same_day_are_never_merged(self):
        findings = compare("gateway", {("a", DAY): D("1")}, {("b", DAY): D("1")}, self.TOLERANCE)

        assert sorted((f.tenant, f.drift) for f in findings) == [("a", D("-1")), ("b", D("1"))]


class TestAttributingTheGateway:
    def test_a_tenant_is_found_by_hashing_the_ones_this_database_knows(self):
        raw = {(end_user_id("acme"), DAY): D("1.5"), (end_user_id("globex"), DAY): D("2")}

        attributed, unattributed = attribute_gateway(raw, ["acme", "globex"])

        assert attributed == {("acme", DAY): D("1.5"), ("globex", DAY): D("2")}
        assert unattributed == 0

    def test_spend_for_an_id_in_the_apps_format_that_no_known_tenant_hashes_to_is_still_a_finding(self):
        # The worst shape a lost meter can take: a tenant with spend at the gateway and no events and no ledger rows at all.
        stranger = "tenant_0123456789abcdef"

        attributed, unattributed = attribute_gateway({(stranger, DAY): D("4")}, ["acme"])

        ((name, day),) = attributed
        assert stranger in name and "no tenant in this database" in name and day == DAY
        assert unattributed == 0

    def test_no_identity_and_another_applications_users_are_reported_but_never_drift(self):
        raw = {("", DAY): D("0.25"), ("some-other-app-user", DAY): D("0.75")}

        attributed, unattributed = attribute_gateway(raw, ["acme"])

        assert attributed == {}
        assert unattributed == D("1.00")

    def test_one_tenants_days_stay_separate_and_sum_per_day(self):
        raw = {(end_user_id("acme"), DAY): D("1"), (end_user_id("acme"), NEXT): D("2")}

        attributed, _ = attribute_gateway(raw, ["acme"])

        assert attributed == {("acme", DAY): D("1"), ("acme", NEXT): D("2")}


def test_the_window_type_is_inclusive_at_both_ends_like_the_gateways_own_filter():
    window = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC))

    assert not window.empty  # a single instant is a window, and the gateway reads `>=` start and `<=` end


@pytest.mark.parametrize("kind", ["gateway", "uncharged"])
def test_every_kind_of_finding_explains_itself_in_both_directions(kind):
    meanings = {Finding(kind, "acme", DAY, D("0"), drift).meaning for drift in (D("1"), D("-1"))}

    assert all(meanings)
