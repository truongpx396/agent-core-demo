"""The set of routes `app/api/main.py` actually serves.

The suite calls handlers as plain functions, so a router that is defined but never passed to
`include_router` leaves every handler test green while the endpoint answers 404. OpenAPI is built
from what is really mounted, so this pins that set. Adding or removing an endpoint means editing
EXPECTED here on purpose: it is the one place the whole HTTP surface is listed.
"""
from fastapi.testclient import TestClient

from app.api import main as api

EXPECTED = {
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/health/ready"),
    ("GET", "/usage"),
    ("POST", "/chat/stream/queued"),
    ("POST", "/chat/resume"),
    ("POST", "/chat/cancel"),
    ("GET", "/chat/sessions"),
    ("GET", "/chat/sessions/{thread_id}/messages"),
    ("GET", "/chat/sessions/{thread_id}/pending_approval"),
    ("POST", "/ingest/upload"),
    ("GET", "/ingest/stream/{job_id}"),
}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def test_every_expected_endpoint_is_mounted_and_nothing_else_is():
    mounted = {
        (method.upper(), path)
        for path, operations in api.app.openapi()["paths"].items()
        for method in operations
        if method in HTTP_METHODS
    }
    assert mounted == EXPECTED, f"missing: {sorted(EXPECTED - mounted)}; unexpected: {sorted(mounted - EXPECTED)}"


def test_the_identity_free_endpoints_answer_through_the_real_app():
    # Not through the handler function: through routing, so a bad mount or a UI path that no longer
    # resolves from the router module's own directory shows up here. (No `with`: the lifespan, which
    # needs real services, never runs.)
    client = TestClient(api.app)
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "<html" in page.text.lower()
    assert client.get("/health").status_code == 200
