"""A stand-in for the one Stripe endpoint the adapter calls, `POST /v1/checkout/sessions`, as an `httpx.MockTransport`.

It encodes ONLY behaviour Stripe documents (docs.stripe.com/api/idempotent_requests and /api/checkout/sessions/create): the secret key
as a bearer token, the `Idempotency-Key` header, the first result replayed for the same key and the same parameters, and an
error when the same key is reused with DIFFERENT parameters. It is a stand-in and proves nothing about Stripe itself: that the
real API behaves the same is checked, with the real sandbox, by tests/provider_sandbox/test_stripe_sandbox.py, which asserts these very
behaviours against Stripe and so keeps this file honest.
"""
from urllib.parse import parse_qsl

import httpx

API_KEY = "sk_test_contract"


class FakeStripeApi:
    def __init__(self, api_key: str = API_KEY):
        self.api_key = api_key
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, str]] = []  # the decoded body of each request that reached the endpoint, in order
        self.sessions: dict[str, dict] = {}  # every session it created, by id
        self._by_key: dict[str, tuple[dict[str, str], dict]] = {}
        self._script: list[httpx.Response] = []
        self._n = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def script(self, *responses: httpx.Response) -> None:
        """The next requests get these responses instead of the documented behaviour (a 500, a 429, a malformed body ...)."""
        self._script.extend(responses)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._script:
            return self._script.pop(0)
        if request.method != "POST" or request.url.path != "/v1/checkout/sessions":
            return _error(404, "invalid_request_error", "resource_missing")
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return _error(401, "invalid_request_error", None)
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.forms.append(form)
        key = request.headers.get("idempotency-key")
        if key in self._by_key:
            first_form, first_response = self._by_key[key]
            if first_form != form:
                return _error(400, "idempotency_error", None)
            return httpx.Response(200, json=first_response)
        self._n += 1
        session = {
            "id": f"cs_test_{self._n:04d}",
            "object": "checkout.session",
            "mode": form.get("mode"),
            "status": "open",
            "payment_status": "unpaid",
            "customer": form.get("customer"),
            "payment_intent": None,  # null until the session completes (docs: the response example)
            "metadata": {k[len("metadata["):-1]: v for k, v in form.items() if k.startswith("metadata[")},
            "url": f"https://checkout.stripe.test/c/pay/cs_test_{self._n:04d}",
        }
        self.sessions[session["id"]] = session
        if key:
            self._by_key[key] = (form, session)
        return httpx.Response(200, json=session)


def _error(status: int, kind: str, code: str | None) -> httpx.Response:
    error = {"type": kind, "message": "a human message that must never be repeated by the adapter, sk_test_****abcd"}
    if code:
        error["code"] = code
    return httpx.Response(status, json={"error": error})
