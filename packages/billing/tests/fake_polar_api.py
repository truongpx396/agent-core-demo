"""A stand-in for the two Polar endpoints the adapter calls, `POST /v1/checkouts/` and `GET /v1/checkouts/`, as an `httpx.MockTransport`.

It encodes ONLY behaviour observed against Polar's real sandbox (2026-10-08) or stated by its SDK, and the most important line is the one
that is NOT here: **a sent `Idempotency-Key` header is ignored**, so the same body twice makes two sessions. That is why the adapter has to
reconcile by listing. The rest: the secret token as a bearer, a 201 with the session on create, metadata echoed on create AND in the list,
the list filtered by `customer_id` and `product_id`, newest first, capped by `limit`, and a 422 whose `detail` is a list of
`{loc, msg, type}`. It is a stand-in and proves nothing about Polar itself: that the real API behaves the same is checked, with the real
sandbox, by tests/provider_sandbox/test_polar_sandbox.py, which asserts these very behaviours against Polar and so keeps this file honest.
"""
import json
import uuid

import httpx

TOKEN = "polar_oat_contract"


class FakePolarApi:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []  # the decoded JSON body of each create that reached the endpoint, in order
        self.sessions: dict[str, dict] = {}  # every session it created, by id, in creation order
        self._script: list[httpx.Response] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def script(self, *responses: httpx.Response) -> None:
        """The next requests get these responses instead of the observed behaviour (a 500, a 429, a malformed body ...)."""
        self._script.extend(responses)

    def set_status(self, session_ref: str, status: str) -> None:
        self.sessions[session_ref]["status"] = status

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._script:
            return self._script.pop(0)
        if request.url.path != "/v1/checkouts/":
            return httpx.Response(404, json={"error": "ResourceNotFound", "detail": "Not found"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "invalid_token", "error_description": "a human message that must never be repeated, polar_oat_****abcd"})
        if request.method == "GET":
            return self._list(request)
        if request.method == "POST":
            return self._create(request)
        return httpx.Response(405, json={"detail": "Method Not Allowed"})

    def _create(self, request: httpx.Request) -> httpx.Response:
        # NOTE: no look at `Idempotency-Key`. Polar ignores it (verified against the sandbox: same body and key, two sessions).
        body = json.loads(request.content)
        products = body.get("products")
        if not isinstance(products, list) or not products:
            return httpx.Response(422, json={"detail": [{"loc": ["body", "products"], "msg": "a human message that must never be repeated", "type": "missing"}]})
        self.bodies.append(body)
        ref = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fake-polar-session-{len(self.sessions)}"))
        session = {
            "id": ref, "status": "open", "url": f"https://sandbox.polar.sh/checkout/polar_c_{len(self.sessions):04d}",
            "metadata": body.get("metadata") or {}, "customer_id": body.get("customer_id"), "product_id": products[0], "products": [{"id": p} for p in products],
            "success_url": body.get("success_url"), "return_url": body.get("return_url"), "total_amount": 1000, "currency": "usd",
            "expires_at": "2026-10-09T14:09:16.330828Z",
        }
        self.sessions[ref] = session
        return httpx.Response(201, json=session)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        items = [
            s for s in reversed(list(self.sessions.values()))  # newest first, as `sorting=-created_at` asks
            if (not params.get("customer_id") or s["customer_id"] == params["customer_id"])
            and (not params.get("product_id") or s["product_id"] == params["product_id"])
        ]
        limit = int(params.get("limit", 10))
        return httpx.Response(200, json={"items": items[:limit], "pagination": {"total_count": len(items), "max_page": 1}})
