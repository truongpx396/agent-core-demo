"""A stand-in for LiteLLM's `GET /spend/logs/v2`, built from what its SOURCE says (litellm 1.104,
`litellm/proxy/spend_tracking/spend_management_endpoints.py::ui_view_spend_logs`), not from what its docs imply:

  * `start_date` and `end_date` are REQUIRED, UTC, `YYYY-MM-DD HH:MM:SS` (or a bare date), and both ends are INCLUSIVE on `startTime`;
  * `page` is 1-based and `page_size` at most 1000 (a larger one is a 422 from the real server);
  * the response is `{"data": [...], "total", "page", "page_size", "total_pages", "total_is_capped"}` and `total` is clamped to
    10 000, so a window with more rows than that cannot be paged by its own `total_pages`;
  * the sort has no tie-breaker, so rows with an equal `startTime` have no guaranteed order between pages (kept stable here; the
    reconciliation de-duplicates by `request_id` and checks the response's own `total`, which its tests drive directly);
  * the endpoint is an admin view: any other key reads nothing useful, modelled as a 401.

A test that fed the reconciliation a hand-made response in another shape would prove nothing about the real gateway.
"""
from datetime import UTC, datetime

import httpx

API_KEY = "sk-test-master"
BASE_URL = "http://gateway.test"
TOTAL_CAP = 10_000


def spend_row(end_user: str | None, when: datetime, spend: float, request_id: str, *, naive: bool = False) -> dict:
    """One spend-log row, with only the columns the reconciliation reads plus a few it must ignore."""
    stamp = when.astimezone(UTC).replace(tzinfo=None).isoformat() if naive else when.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "request_id": request_id, "end_user": end_user, "spend": spend, "startTime": stamp, "model": "chat",
        "total_tokens": 10, "status": "success", "call_type": "acompletion",
    }


class FakeGateway:
    def __init__(self, rows: list[dict], *, key: str = API_KEY, cap_total: bool = True):
        self.rows = rows
        self.key = key
        self.cap_total = cap_total
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": "not an admin"})
        if request.url.path != "/spend/logs/v2":
            return httpx.Response(404, json={"detail": "Not Found"})
        query = request.url.params
        if "start_date" not in query or "end_date" not in query:
            return httpx.Response(400, json={"error": "Start date and end date are required"})
        page, size = int(query.get("page", 1)), int(query.get("page_size", 50))
        if page < 1 or not 1 <= size <= 1000:
            return httpx.Response(422, json={"detail": "page and page_size out of range"})
        start, end = (self._parse(query["start_date"]), self._parse(query["end_date"]))
        inside = sorted(
            (row for row in self.rows if start <= self._parse(row["startTime"]) <= end), key=lambda row: self._parse(row["startTime"])
        )
        total = min(len(inside), TOTAL_CAP + 1 if not self.cap_total else TOTAL_CAP)
        return httpx.Response(
            200,
            json={
                "data": inside[(page - 1) * size : page * size], "total": total, "page": page, "page_size": size,
                "total_pages": (total + size - 1) // size, "total_is_capped": len(inside) > TOTAL_CAP,
            },
        )

    @staticmethod
    def _parse(text: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL, headers={"Authorization": f"Bearer {self.key}"}, transport=httpx.MockTransport(self._handle)
        )
