"""The trusted-identity boundary of the HTTP API (Constitution Principle I):
a request that does not carry BOTH identity headers must never reach a
handler. FastAPI turns a missing required header into a 422 before the handler
runs, so "fail closed" lives in the *shape* of `get_ctx`'s signature
(app/api/main.py).

This is the first test that actually sends a request without them. The
neighbouring `test_sends_the_trusted_identity_headers` only checks that the
demo UI's HTML mentions the header names, which says nothing about what the
server does with a request that lacks them.

`TestClient(app)` is constructed WITHOUT `with`, so the lifespan (real
checkpointer, real Qdrant) never runs — a 422 is produced by request
validation alone and no handler body executes.
"""
import pytest
from fastapi.testclient import TestClient

from app.api import main as api
from app.core.security import valid_ctx

client = TestClient(api.app)

ENDPOINTS = [
    ("GET", "/usage", None),
    ("GET", "/chat/sessions", None),
    ("GET", "/chat/sessions/t1/messages", None),
    ("GET", "/chat/sessions/t1/pending_approval", None),
    ("POST", "/chat/stream/queued", {"message": "hi", "thread_id": "t1"}),
    ("POST", "/chat/resume", {"thread_id": "t1", "approved": True}),
    ("POST", "/chat/cancel", {"thread_id": "t1"}),
]

HEADER_SETS = {
    "neither": ({}, {"x-tenant-id", "x-principal-id"}),
    "no-principal": ({"X-Tenant-Id": "acme"}, {"x-principal-id"}),
    "no-tenant": ({"X-Principal-Id": "alice"}, {"x-tenant-id"}),
}


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in ENDPOINTS])
@pytest.mark.parametrize("headers,missing", HEADER_SETS.values(), ids=HEADER_SETS.keys())
def test_a_request_missing_an_identity_header_is_rejected_before_the_handler(
    method, path, body, headers, missing
):
    response = client.request(method, path, json=body, headers=headers)

    assert response.status_code == 422
    reported = {error["loc"][-1] for error in response.json()["detail"] if error["loc"][0] == "header"}
    assert reported == missing, "the 422 must name exactly the headers that were absent"


async def test_an_empty_identity_value_passes_the_header_check_but_is_not_a_valid_ctx():
    """Pins what the boundary actually does with an EMPTY value, because it is
    easy to assume the opposite: `get_ctx` accepts an empty string (the header
    is present), so the request is NOT rejected at the HTTP layer. Isolation
    still holds because every consumer fails closed on `valid_ctx` — the graph
    (`route_after_validation`), every ctx-aware tool, the session and cache
    lookups. A stricter boundary (reject empty with a 422) would be a
    behavior change the load test's invalid-identity scenario relies on being
    absent, so it is recorded here rather than silently assumed."""
    ctx = await api.get_ctx(x_tenant_id="", x_principal_id="")

    assert ctx == {"tenant": "", "principal": "", "claims": {}}
    assert valid_ctx(ctx) is False
