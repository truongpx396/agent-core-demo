"""`get_ctx` by mode (specs/018, task T058): in identity mode the caller is who the front door's signed identity says, and nobody else.

Two levels, both over the real dependency, verifier, key cache and exception handler, with only the front door faked (tests/identity_support.py):
  * a one-route app that returns the ctx `get_ctx` built, so what is asserted is exactly the dependency's answer and the shape of its refusals;
  * the REAL application's routes, to prove that no route that needs a caller can be reached in identity mode without one.

`TestClient(app)` is built WITHOUT `with`, so no lifespan runs (no real checkpointer, no real key fetch) and the handlers below the dependency
never execute: a refusal is produced by the dependency alone. The hermetic suite's autouse fixture puts every test in header mode; the ones
here that mean identity mode ask for `identity_mode`.
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import deps, identity
from app.api import main as api
from app.core import metrics
from app.core.errors import AUTH_ERRORS, ErrorCode
from app.core.internal_identity import InternalIdentityVerifier, InternalKeys
from app.core.security import SecurityCtx, valid_ctx
from tests.conftest import metric_value
from tests.identity_support import (
    GRACE,
    MIN_REFRESH,
    TTL,
    URL,
    Clock,
    FrontDoor,
    claims,
)

UNAUTHENTICATED = ErrorCode.UNAUTHENTICATED
UNAVAILABLE = ErrorCode.AUTH_UNAVAILABLE


def _who_is_calling_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(identity.AuthRefused, identity.auth_refused_handler)

    @app.get("/who")
    async def who(ctx: SecurityCtx = Depends(deps.get_ctx)) -> SecurityCtx:
        return ctx

    return app


@pytest.fixture
def front_door(identity_mode, monkeypatch):
    """The API in identity mode with a verifier whose keys come from a fake front door. Returns (door, clock)."""
    door, clock = FrontDoor(), Clock()
    keys = InternalKeys(URL, ttl_seconds=TTL, stale_grace_seconds=GRACE, min_refresh_seconds=MIN_REFRESH, client=door.client(), clock=clock)
    monkeypatch.setattr(identity, "_verifier", InternalIdentityVerifier(keys))
    return door, clock


def _call(headers: dict) -> object:
    return TestClient(_who_is_calling_app()).get("/who", headers=headers)


# --- identity mode: the caller is the front door's word --------------------------------------------------------------------------------


def test_a_valid_identity_is_the_whole_ctx(front_door):
    door, _clock = front_door

    response = _call({"X-Internal-Identity": door.signer.sign(claims(tenant="acme", sub="alice"))})

    assert response.status_code == 200
    assert response.json() == {"tenant": "acme", "principal": "alice", "claims": {}}
    assert valid_ctx(response.json())


def test_the_clients_own_identity_headers_are_ignored_not_merged_not_preferred(front_door):
    """The point of the whole feature: a person cannot become another tenant by saying so."""
    door, _clock = front_door

    response = _call(
        {"X-Internal-Identity": door.signer.sign(claims(tenant="acme", sub="alice")), "X-Tenant-Id": "victim-co", "X-Principal-Id": "the-boss"}
    )

    assert response.json() == {"tenant": "acme", "principal": "alice", "claims": {}}


def test_the_clients_headers_alone_are_not_an_identity(front_door):
    response = _call({"X-Tenant-Id": "victim-co", "X-Principal-Id": "the-boss"})

    assert response.status_code == 401 and response.json()["code"] == "unauthenticated"


def test_a_request_with_no_identity_is_refused_401_in_the_front_doors_own_envelope(front_door):
    before = metric_value(metrics.agent_api_identity_total, outcome="missing")

    response = _call({})

    status, message = AUTH_ERRORS[UNAUTHENTICATED]
    assert response.status_code == status == 401
    assert response.json() == {"code": "unauthenticated", "message": message, "details": {}}
    assert response.headers["www-authenticate"] == 'Bearer realm="agent-core-api"'
    assert metric_value(metrics.agent_api_identity_total, outcome="missing") == before + 1


@pytest.mark.parametrize("why", ["expired", "forged", "wrong_audience", "garbage"])
def test_every_bad_identity_is_the_same_401_so_a_caller_learns_nothing_about_why(front_door, why):
    door, _clock = front_door
    import time

    now = int(time.time())
    token = {
        "expired": lambda: door.signer.sign(claims(iat=now - 120, nbf=now - 120, exp=now - 60)),
        "forged": lambda: door.signer.sign(claims(), kid="never-published"),
        "wrong_audience": lambda: door.signer.sign(claims(aud="somebody-else")),
        "garbage": lambda: "not.a.token",
    }[why]()

    response = _call({"X-Internal-Identity": token})

    assert response.status_code == 401
    assert response.json() == _call({}).json(), "the body is the same one a request with no identity gets"
    assert token not in response.text


def test_when_the_front_doors_keys_cannot_be_confirmed_the_answer_is_503_not_401(front_door):
    """The caller did nothing wrong, so it must not look like an expired login: retrying shortly is the right thing to do."""
    door, clock = front_door
    token = door.signer.sign(claims())
    assert _call({"X-Internal-Identity": token}).status_code == 200
    door.fail_with = 503
    clock.advance(TTL + GRACE + 1)
    before = metric_value(metrics.agent_api_identity_total, outcome="keys_unavailable")

    response = _call({"X-Internal-Identity": token})

    status, message = AUTH_ERRORS[UNAVAILABLE]
    assert response.status_code == status == 503
    assert response.json() == {"code": "auth_unavailable", "message": message, "details": {}}
    assert response.headers["retry-after"] == "30"
    assert metric_value(metrics.agent_api_identity_total, outcome="keys_unavailable") == before + 1


def test_a_request_with_no_identity_is_a_401_even_while_the_keys_are_unavailable(front_door):
    """Whether a header was sent does not depend on the keys."""
    door, clock = front_door
    _call({"X-Internal-Identity": door.signer.sign(claims())})
    door.fail_with = 503
    clock.advance(TTL + GRACE + 1)

    assert _call({}).status_code == 401


# --- header mode: today's behaviour, unchanged ------------------------------------------------------------------------------------------


def test_header_mode_still_builds_the_ctx_from_the_two_headers():
    response = _call({"X-Tenant-Id": "acme", "X-Principal-Id": "alice"})

    assert response.json() == {"tenant": "acme", "principal": "alice", "claims": {}}


def test_header_mode_ignores_an_internal_identity_it_was_not_asked_to_check():
    """Header mode is the old contract: the two headers, nothing else. (Behind the real front door the mode is identity, so this never matters.)"""
    door = FrontDoor()

    response = _call({"X-Tenant-Id": "acme", "X-Principal-Id": "alice", "X-Internal-Identity": door.signer.sign(claims(tenant="other"))})

    assert response.json()["tenant"] == "acme"


@pytest.mark.parametrize("headers,missing", [
    ({}, {"x-tenant-id", "x-principal-id"}),
    ({"X-Tenant-Id": "acme"}, {"x-principal-id"}),
    ({"X-Principal-Id": "alice"}, {"x-tenant-id"}),
], ids=["neither", "no-principal", "no-tenant"])
def test_header_mode_still_names_exactly_the_headers_that_were_absent_with_a_422(headers, missing):
    response = _call(headers)

    assert response.status_code == 422
    assert {e["loc"][-1] for e in response.json()["detail"] if e["loc"][0] == "header"} == missing


# --- the real application ---------------------------------------------------------------------------------------------------------------

# (method, path, body) of every route that needs a caller; the same list tests/api/test_identity_headers.py walks in header mode.
CALLER_ROUTES = [
    ("GET", "/usage", None),
    ("GET", "/chat/sessions", None),
    ("GET", "/chat/sessions/t1/messages", None),
    ("GET", "/chat/sessions/t1/pending_approval", None),
    ("POST", "/chat/stream/queued", {"message": "hi", "thread_id": "t1"}),
    ("POST", "/chat/resume", {"thread_id": "t1", "approved": True}),
    ("POST", "/chat/cancel", {"thread_id": "t1"}),
    ("POST", "/ingest/upload", None),
]


@pytest.mark.parametrize("method,path,body", CALLER_ROUTES, ids=[f"{m} {p}" for m, p, _ in CALLER_ROUTES])
def test_no_route_that_needs_a_caller_can_be_reached_in_identity_mode_without_an_identity(front_door, method, path, body):
    response = TestClient(api.app).request(method, path, json=body, headers={"X-Tenant-Id": "victim-co", "X-Principal-Id": "the-boss"})

    assert response.status_code == 401, f"{method} {path} answered {response.status_code} to a request that carries only the client's own headers"
    assert response.json()["code"] == "unauthenticated"


def test_the_pages_that_need_no_caller_stay_open_in_identity_mode(front_door):
    client = TestClient(api.app)

    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_the_real_app_answers_a_refusal_in_the_envelope_because_the_handler_is_registered():
    assert api.app.exception_handlers[identity.AuthRefused] is identity.auth_refused_handler


# The documented routes that do NOT build a ctx. The first three are public pages; the last is a job's event stream, which the front door
# verifies (class `read`) but which carries no tenant-scoped data of its own that the API looks up by caller.
NO_CALLER_NEEDED = {("GET", "/"), ("GET", "/health"), ("GET", "/health/ready"), ("GET", "/ingest/stream/{job_id}")}


def test_a_route_added_without_get_ctx_fails_here_until_someone_decides_it_needs_no_caller():
    """`get_ctx` declares `X-Internal-Identity`, so every route that depends on it shows that header in OpenAPI. A new route without it is a
    route that serves whoever asks in BOTH modes; it has to be listed above on purpose."""
    uses_get_ctx, without = set(), set()
    for path, operations in api.app.openapi()["paths"].items():
        for method, operation in operations.items():
            names = {parameter["name"].lower() for parameter in operation.get("parameters", [])}
            (uses_get_ctx if "x-internal-identity" in names else without).add((method.upper(), path))

    assert without == NO_CALLER_NEEDED, f"routes with no caller that are not listed: {sorted(without - NO_CALLER_NEEDED)}; listed but now using get_ctx: {sorted(NO_CALLER_NEEDED - without)}"
    covered = {(m, p.replace("t1", "{thread_id}")) for m, p, _ in CALLER_ROUTES}
    assert covered == uses_get_ctx, f"the routes tested above and the routes that need a caller differ: {sorted(covered ^ uses_get_ctx)}"
