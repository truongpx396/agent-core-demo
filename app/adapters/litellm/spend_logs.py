"""The `GatewaySpendLog` adapter (`app/ports/cost.py`): the LLM gateway's spend log, read over its admin API.

The read is `GET /spend/logs/v2` with `start_date`/`end_date`, paginated (checked against LiteLLM 1.104: UTC window on `startTime`, both
ends inclusive, `page_size` at most 1000, `total` capped at 10 000 so the page count never comes from it). The sort has no tie-breaker, so
rows are de-duplicated by `request_id`, and a `total` below the cap must equal the rows collected or the read is INCOMPLETE. An incomplete
read is never compared by the reconciliation (`app/agent/cost/usage_reconcile.py`), since a truncated sum would page someone for every tenant.
Ceiling: `USAGE_RECONCILE_GATEWAY_MAX_PAGES`.

It needs the gateway admin key (`LITELLM_MASTER_KEY`, read from the environment by the caller, never stored here), since spend logs are an
admin view: the caller hands this adapter an `httpx.AsyncClient` that already carries it.
"""
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from app.core.config import USAGE_RECONCILE_GATEWAY_MAX_PAGES
from app.ports.cost import GatewaySpend, Window

ZERO = Decimal(0)
GATEWAY_PAGE_SIZE = 1000  # the most `/spend/logs/v2` accepts (`le=1000`)
GATEWAY_PATH = "/spend/logs/v2"
_GATEWAY_TOTAL_CAP = 10_000  # `SPEND_LOGS_PAGINATION_COUNT_CAP` in LiteLLM: a `total` at or above it is a floor, not a count


def _row_day_and_spend(row: Mapping) -> tuple[str, date, Decimal, str]:
    try:
        start = datetime.fromisoformat(str(row["startTime"]).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return str(row.get("end_user") or ""), start.astimezone(UTC).date(), Decimal(str(row.get("spend") or 0)), str(row["request_id"])
    except (KeyError, ValueError, ArithmeticError, AttributeError, TypeError) as exc:
        raise ValueError(f"a spend-log row the reconciliation cannot read ({type(exc).__name__}): the gateway's format may have changed") from None


def _gateway_date(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def fetch_gateway_spend(client: httpx.AsyncClient, window: Window, *, max_pages: int = USAGE_RECONCILE_GATEWAY_MAX_PAGES) -> GatewaySpend:
    """Every spend-log row in the window, summed by end user and UTC day (see the module doc for the endpoint's verified
    behaviour). Raises on an HTTP error or a response that is not the documented shape; returns `complete=False`, with
    the reason, when the read cannot be trusted to be whole."""
    sums: dict[tuple[str, date], Decimal] = {}
    seen: set[str] = set()
    total: int | None = None
    for page in range(1, max_pages + 1):
        response = await client.get(
            GATEWAY_PATH,
            params={
                "start_date": _gateway_date(window.start), "end_date": _gateway_date(window.end), "page": page,
                "page_size": GATEWAY_PAGE_SIZE, "sort_by": "startTime", "sort_order": "asc",
            },
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError(f"the gateway's {GATEWAY_PATH} answered with an unexpected shape: its API may have changed")
        if total is None:
            total = body.get("total") if isinstance(body.get("total"), int) else None
        for row in data:
            end_user, day, amount, request_id = _row_day_and_spend(row)
            if request_id in seen:
                continue
            seen.add(request_id)
            sums[(end_user, day)] = sums.get((end_user, day), ZERO) + amount
        if len(data) < GATEWAY_PAGE_SIZE:
            if total is not None and total < _GATEWAY_TOTAL_CAP and total != len(seen):
                return GatewaySpend(sums, len(seen), False, f"it reported {total} rows and {len(seen)} were read: the log changed while it was being read")
            return GatewaySpend(sums, len(seen), True)
    return GatewaySpend(sums, len(seen), False, f"more than {max_pages * GATEWAY_PAGE_SIZE} rows (USAGE_RECONCILE_GATEWAY_MAX_PAGES)")



class HttpGatewaySpendLog:
    """The adapter: one `httpx.AsyncClient`, already pointed at the gateway and carrying its admin key."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def spend(self, window: Window, *, max_pages: int = USAGE_RECONCILE_GATEWAY_MAX_PAGES) -> GatewaySpend:
        return await fetch_gateway_spend(self._client, window, max_pages=max_pages)
