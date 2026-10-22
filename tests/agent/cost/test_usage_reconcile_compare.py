"""The pure half of the reconciliation (app/agent/cost/usage_reconcile.py): who is allowed to differ by how much, which window is read, how a
gateway end user (a person, or the older one-way tenant id) becomes a tenant, and what counts as drift. No database and no gateway: the reading of each is in
test_reconcile_gateway.py and packages/billing/tests/integration/test_reconcile_real_postgres.py."""
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D

from app.agent.cost.usage_reconcile import (
    Finding,
    Tolerance,
    Window,
    attribute_gateway,
    compare,
    default_window,
)
from app.core.gateway import end_user_id, legacy_end_user_id

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
    def test_a_tenant_is_read_off_the_end_user_so_no_list_of_known_tenants_is_needed(self):
        raw = {(end_user_id("acme", "alice"), DAY): D("1.5"), (end_user_id("globex", "carol"), DAY): D("2")}

        attributed, unattributed = attribute_gateway(raw, [])

        assert attributed == {("acme", DAY): D("1.5"), ("globex", DAY): D("2")}
        assert unattributed == 0

    def test_the_people_of_one_tenant_are_summed_into_its_day(self):
        """The gateway has one end user per person and the reconciliation is per tenant."""
        raw = {
            (end_user_id("acme", "alice"), DAY): D("1"),
            (end_user_id("acme", "bob"), DAY): D("2"),
            (end_user_id("acme", "bob"), NEXT): D("4"),
            (end_user_id("globex", "alice"), DAY): D("8"),  # the same principal string in another tenant is another person
        }

        attributed, _ = attribute_gateway(raw, ["acme", "globex"])

        assert attributed == {("acme", DAY): D("3"), ("acme", NEXT): D("4"), ("globex", DAY): D("8")}

    def test_a_tenant_with_spend_at_the_gateway_and_no_events_is_attributed_by_name_so_compare_reports_it(self):
        # The worst shape a lost meter can take: spend at the gateway, no event left. It needs no special case: the tenant is in the id,
        # and `compare` reports a day present on one side only as a difference from zero.
        attributed, _ = attribute_gateway({(end_user_id("ghost", "alice"), DAY): D("4")}, ["acme"])

        findings = compare("gateway", {("acme", DAY): D("1")}, attributed, Tolerance(usd=D("0.01"), pct=D("0")))

        assert ("ghost", D("4")) in [(f.tenant, f.drift) for f in findings]

    def test_a_tenant_or_principal_containing_a_delimiter_is_still_attributed_to_the_right_tenant(self):
        raw = {(end_user_id("a/user:b", "c"), DAY): D("1"), (end_user_id("a", "b/user:c"), DAY): D("2")}

        attributed, _ = attribute_gateway(raw, [])

        assert attributed == {("a/user:b", DAY): D("1"), ("a", DAY): D("2")}

    # --- spend-log rows written before the end user named the person ---

    def test_an_older_tenant_level_id_is_found_by_hashing_the_tenants_this_database_knows(self):
        raw = {(legacy_end_user_id("acme"), DAY): D("1.5"), (legacy_end_user_id("globex"), DAY): D("2")}

        attributed, unattributed = attribute_gateway(raw, ["acme", "globex"])

        assert attributed == {("acme", DAY): D("1.5"), ("globex", DAY): D("2")}
        assert unattributed == 0

    def test_both_generations_of_id_in_one_window_land_in_the_same_tenant_day(self):
        """The window can straddle the day the end user started naming the person."""
        raw = {(legacy_end_user_id("acme"), DAY): D("1"), (end_user_id("acme", "alice"), DAY): D("2")}

        attributed, _ = attribute_gateway(raw, ["acme"])

        assert attributed == {("acme", DAY): D("3")}

    def test_an_older_id_that_no_known_tenant_hashes_to_is_still_a_finding(self):
        stranger = "tenant_0123456789abcdef"

        attributed, unattributed = attribute_gateway({(stranger, DAY): D("4")}, ["acme"])

        ((name, day),) = attributed
        assert stranger in name and "no tenant in this database" in name and day == DAY
        assert unattributed == 0

    def test_no_identity_and_another_applications_users_are_reported_but_never_drift(self):
        raw = {
            ("", DAY): D("0.25"),
            ("some-other-app-user", DAY): D("0.25"),
            ("tenant:looks-like-ours", DAY): D("0.25"),  # starts the same way but is not the canonical id this app writes
            ("tenant:acme/user:%61lice", DAY): D("0.25"),  # a non-canonical spelling of ours
        }

        attributed, unattributed = attribute_gateway(raw, ["acme"])

        assert attributed == {}
        assert unattributed == D("1.00")


def test_the_window_type_is_inclusive_at_both_ends_like_the_gateways_own_filter():
    window = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC))

    assert not window.empty  # a single instant is a window, and the gateway reads `>=` start and `<=` end


def test_a_finding_explains_itself_in_both_directions():
    meanings = {Finding("gateway", "acme", DAY, D("0"), drift).meaning for drift in (D("1"), D("-1"))}

    assert len(meanings) == 2 and all(meanings)
