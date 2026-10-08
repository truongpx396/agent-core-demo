"""Reading the gateway's spend log (app/billing/reconcile.py::fetch_gateway_spend) against a stand-in built from LiteLLM's own source
(tests/billing/fake_gateway.py): the request it makes, the pages it walks, and every way the read can be untrustworthy.

The rule these pin: a read that is not whole is NEVER compared. A truncated sum would show every tenant as under-metered and page
someone for nothing, so an unsure read comes back `complete=False` with its reason, and a read that is plainly wrong raises."""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from app.billing.reconcile import GATEWAY_PAGE_SIZE, Window, fetch_gateway_spend
from tests.billing.fake_gateway import API_KEY, FakeGateway, spend_row

START = datetime(2026, 10, 6, 0, 0, 0, tzinfo=UTC)
WINDOW = Window(START, datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC))
DAY, NEXT = date(2026, 10, 6), date(2026, 10, 7)


def rows(n: int, *, end_user: str = "tenant_a", spend: float = 0.5, start: datetime = START) -> list[dict]:
    return [spend_row(end_user, start + timedelta(seconds=i), spend, f"req-{end_user}-{i}") for i in range(n)]


async def fetch(gateway: FakeGateway, window: Window = WINDOW, **kwargs):
    async with gateway.client() as client:
        return await fetch_gateway_spend(client, window, **kwargs)


class TestTheRequest:
    async def test_it_asks_for_the_window_in_the_gateways_own_format_with_the_admin_key(self):
        gateway = FakeGateway(rows(3))

        await fetch(gateway)

        (request,) = gateway.requests
        assert request.url.path == "/spend/logs/v2"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert dict(request.url.params) == {
            "start_date": "2026-10-06 00:00:00", "end_date": "2026-10-07 12:00:00", "page": "1",
            "page_size": str(GATEWAY_PAGE_SIZE), "sort_by": "startTime", "sort_order": "asc",
        }
        assert GATEWAY_PAGE_SIZE == 1000  # the most the endpoint accepts: a larger page is a 422

    async def test_a_window_in_another_zone_is_sent_as_utc(self):
        from datetime import timezone

        gateway = FakeGateway([])
        plus_nine = timezone(timedelta(hours=9))

        await fetch(gateway, Window(datetime(2026, 10, 6, 9, 0, tzinfo=plus_nine), datetime(2026, 10, 7, 9, 0, tzinfo=plus_nine)))

        assert gateway.requests[0].url.params["start_date"] == "2026-10-06 00:00:00"


class TestWhatItSums:
    async def test_spend_is_summed_per_end_user_and_utc_day(self):
        gateway = FakeGateway(
            [
                spend_row("tenant_a", datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC), 1.25, "r1"),
                spend_row("tenant_a", datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC), 2.0, "r2"),  # midnight belongs to the NEW day
                spend_row("tenant_a", datetime(2026, 10, 7, 0, 0, 1, tzinfo=UTC), 0.5, "r3"),
                spend_row("tenant_b", datetime(2026, 10, 6, 10, 0, tzinfo=UTC), 0.1, "r4", naive=True),  # a time with no zone is UTC
                spend_row(None, datetime(2026, 10, 6, 10, 0, tzinfo=UTC), 0.2, "r5"),
            ]
        )

        spend = await fetch(gateway)

        assert spend.complete and spend.rows == 5
        assert dict(spend.by_end_user_day) == {
            ("tenant_a", DAY): D("1.25"), ("tenant_a", NEXT): D("2.5"), ("tenant_b", DAY): D("0.1"), ("", DAY): D("0.2"),
        }

    async def test_money_is_read_as_the_decimal_it_was_written_never_through_binary_floating_point(self):
        gateway = FakeGateway([spend_row("tenant_a", START, 0.1, "r1"), spend_row("tenant_a", START, 0.2, "r2")])

        spend = await fetch(gateway)

        assert spend.by_end_user_day[("tenant_a", DAY)] == D("0.3")  # 0.1 + 0.2 as floats is 0.30000000000000004

    async def test_a_row_with_no_spend_counts_as_zero(self):
        gateway = FakeGateway([{**spend_row("tenant_a", START, 0, "r1"), "spend": None}])

        assert (await fetch(gateway)).by_end_user_day[("tenant_a", DAY)] == 0

    async def test_both_ends_of_the_window_are_inclusive_and_nothing_outside_is_read(self):
        gateway = FakeGateway(
            [
                spend_row("tenant_a", START - timedelta(seconds=1), 100, "before"),
                spend_row("tenant_a", START, 1, "first"),
                spend_row("tenant_a", WINDOW.end, 2, "last"),
                spend_row("tenant_a", WINDOW.end + timedelta(seconds=1), 100, "after"),
            ]
        )

        spend = await fetch(gateway)

        assert sum(spend.by_end_user_day.values()) == D("3") and spend.rows == 2


class TestPaging:
    async def test_every_page_is_read_and_summed(self):
        gateway = FakeGateway(rows(2 * GATEWAY_PAGE_SIZE + 500))

        spend = await fetch(gateway)

        assert spend.complete and spend.rows == 2500
        assert sum(spend.by_end_user_day.values()) == D("1250.0")
        assert [r.url.params["page"] for r in gateway.requests] == ["1", "2", "3"]

    async def test_a_window_of_exactly_full_pages_ends_on_the_empty_page_after(self):
        gateway = FakeGateway(rows(GATEWAY_PAGE_SIZE))

        spend = await fetch(gateway)

        assert spend.complete and spend.rows == GATEWAY_PAGE_SIZE and len(gateway.requests) == 2

    async def test_a_row_served_on_two_pages_is_counted_once(self):
        # The endpoint's sort has no tie-breaker, so a row can move between pages while the log is written.
        duplicated = rows(GATEWAY_PAGE_SIZE + 1)
        duplicated[GATEWAY_PAGE_SIZE] = {**duplicated[0], "startTime": duplicated[GATEWAY_PAGE_SIZE]["startTime"]}  # same request id again

        spend = await fetch(FakeGateway(duplicated))

        assert spend.rows == GATEWAY_PAGE_SIZE
        assert spend.by_end_user_day[("tenant_a", DAY)] == D("500.0")

    async def test_past_the_gateways_own_count_cap_the_total_is_a_floor_not_a_check(self):
        # `total` is clamped to 10 000, so a bigger window must not be declared "changed while reading" for disagreeing with it.
        gateway = FakeGateway(rows(10_001, spend=0.0))

        spend = await fetch(gateway)

        assert spend.complete and spend.rows == 10_001

    async def test_a_window_longer_than_the_page_ceiling_is_incomplete_not_short(self):
        gateway = FakeGateway(rows(2500))

        spend = await fetch(gateway, max_pages=2)

        assert not spend.complete
        assert "CREDIT_RECONCILE_GATEWAY_MAX_PAGES" in spend.reason
        assert len(gateway.requests) == 2


class TestAReadItCannotTrust:
    async def test_a_total_that_disagrees_with_the_rows_read_means_the_log_changed_underneath_it(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": rows(2), "total": 5, "page": 1, "page_size": 1000})

        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handler)) as client:
            spend = await fetch_gateway_spend(client, WINDOW)

        assert not spend.complete
        assert "5 rows" in spend.reason and "2 were read" in spend.reason

    @pytest.mark.parametrize("status", [401, 404, 500])
    async def test_a_refusal_raises_instead_of_reading_as_no_spend(self, status):
        gateway = FakeGateway([], key="another-key") if status == 401 else None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "no"})

        transport = httpx.MockTransport(handler) if gateway is None else httpx.MockTransport(gateway._handle)
        async with httpx.AsyncClient(base_url="http://gateway.test", headers={"Authorization": "Bearer wrong"}, transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_gateway_spend(client, WINDOW)

    @pytest.mark.parametrize("body", [[], {"rows": []}, {"data": "nope"}, "text"])
    async def test_an_answer_that_is_not_the_documented_shape_raises(self, body):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(body))

        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="unexpected shape"):
                await fetch_gateway_spend(client, WINDOW)

    @pytest.mark.parametrize("damage", [{"startTime": None}, {"startTime": "yesterday"}, {"spend": "lots"}, {"request_id": None}])
    async def test_a_row_it_cannot_read_raises_naming_the_likely_cause(self, damage):
        # Served as-is: the real column is a timestamp, so the stand-in's own window filter could not even hold such a row.
        row = {**spend_row("tenant_a", START, 1, "r1"), **damage}
        if damage == {"request_id": None}:
            row.pop("request_id")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [row], "total": 1})

        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="format may have changed"):
                await fetch_gateway_spend(client, WINDOW)
