"""Every route the API serves is classified, on purpose, in the front door's route table (specs/018, task T064).

`services/authlimit/routes.json` says, per route, what the front door does with a request: `public` (no token), `read` (verified, never limited),
`cancel` (verified, never limited, stopping a runaway turn must not be throttled), `turn` and `upload` (verified AND charged to the tenant's and the
person's token buckets). A route the table does not list is not unsafe: it falls to the `turn` class, which verifies it and limits it. But that is a
silent choice, and a quiet wrong one for a read-only page (every fetch would burn a turn from a person's bucket of 5). So a route added to the API
without a decision about its class fails here, with the list of what is unclassified.

The Go side pins the same intent from its own end (`TestEveryRouteTheAPIServesIsInTheDefaultTable`); this walks the REAL app, so the two cannot
drift apart. The matcher below is a port of `internal/limiter/routes.go`'s (first match wins; `{name}` is one non-empty segment; a trailing `*` is any
tail; a `*` method is any method).

What this can and cannot see. The documented surface comes from OpenAPI (the technique tests/api/test_routes_mounted.py uses, because FastAPI does
not flatten included routers into `app.routes`); the payment webhook, hidden from OpenAPI on purpose, is read from its router; FastAPI's own pages
are plain routes on the app. A NEW route hidden from OpenAPI inside another router is not seen here. It is still safe (the fallback verifies and
limits it); it is only unclassified.
"""
import json
from pathlib import Path

from app.api import main as api
from credit_billing import http as billing_http

TABLE = json.loads((Path(__file__).resolve().parents[2] / "services" / "authlimit" / "routes.json").read_text())
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# What each documented route must be. Adding an endpoint means deciding its class here, on purpose.
EXPECTED_CLASS = {
    ("GET", "/"): "public",
    ("GET", "/health"): "public",
    ("GET", "/health/ready"): "public",
    ("POST", "/chat/stream/queued"): "turn",
    ("POST", "/chat/resume"): "turn",
    ("POST", "/ingest/upload"): "upload",
    ("POST", "/chat/cancel"): "cancel",
    ("GET", "/chat/sessions"): "read",
    ("GET", "/chat/sessions/{thread_id}/messages"): "read",
    ("GET", "/chat/sessions/{thread_id}/pending_approval"): "read",
    ("GET", "/usage"): "read",
    ("GET", "/ingest/stream/{job_id}"): "read",
}


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/")] if path.strip("/") else []


def _matches(pattern: str, segs: list[str]) -> bool:
    want = _segments(pattern)
    tail = bool(want) and want[-1] == "*"
    if tail:
        want = want[:-1]
    if len(segs) < len(want) or (not tail and len(segs) != len(want)):
        return False
    return all((w.startswith("{") and w.endswith("}") and segs[i] != "") or w == segs[i] for i, w in enumerate(want))


def classify(method: str, path: str) -> tuple[str, bool]:
    """`(class, listed)` the way the Go table answers; `{param}` in an application path stands for one concrete segment."""
    concrete = _segments("/".join("x" if s.startswith("{") else s for s in path.split("/")))
    for rule in TABLE["routes"]:
        if rule["method"] in ("*", method.upper()) and _matches(rule["pattern"], concrete):
            return rule["class"], True
    return TABLE["fallback"], False


def _documented_routes() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in api.app.openapi()["paths"].items()
        for method in operations
        if method in HTTP_METHODS
    }


def test_every_documented_route_is_listed_with_the_class_it_must_have():
    mounted = _documented_routes()
    problems = []
    for method, path in sorted(mounted):
        want = EXPECTED_CLASS.get((method, path))
        if want is None:
            problems.append(f"{method} {path}: no class decided in EXPECTED_CLASS")
            continue
        got, listed = classify(method, path)
        if (got, listed) != (want, True):
            problems.append(f"{method} {path}: the table says {got!r} (listed: {listed}), it must be {want!r}")

    assert not problems, "unclassified or misclassified routes:\n  " + "\n  ".join(problems)
    assert set(EXPECTED_CLASS) == mounted, f"stale entries in EXPECTED_CLASS: {sorted(set(EXPECTED_CLASS) - mounted)}"


def test_the_payment_webhook_is_public_because_the_provider_signs_it_and_holds_no_token():
    routes = [(method, route.path) for route in billing_http.router.routes for method in sorted(route.methods)]

    assert routes, "premise: the billing router has routes"
    for method, path in routes:
        assert classify(method, path) == ("public", True), f"{method} {path}"


def test_fastapis_own_pages_are_listed_as_read_not_left_to_burn_a_persons_turn_bucket():
    own = [(method, route.path) for route in api.app.routes if type(route).__name__ == "Route" for method in sorted(route.methods) if method != "HEAD"]

    assert {path for _, path in own} >= {"/docs", "/redoc", "/openapi.json"}, "premise: the app serves its docs pages"
    for method, path in own:
        assert classify(method, path) == ("read", True), f"{method} {path} is unlisted, so the front door would treat it as a turn"


def test_only_the_three_turn_creating_routes_are_limited():
    """The same set the app's own limiter covered before spec 018, and now the only routes the front door charges to a bucket."""
    limited = {r for r in _documented_routes() if classify(*r)[0] in TABLE["limits"] and classify(*r)[0] != "mcp"}

    assert limited == {("POST", "/chat/stream/queued"), ("POST", "/chat/resume"), ("POST", "/ingest/upload")}


def test_the_port_of_the_matcher_agrees_with_the_go_fallback_for_a_route_nobody_listed():
    assert classify("GET", "/not-a-route") == (TABLE["fallback"], False)
    assert classify("POST", "/chat/cancel/extra") == (TABLE["fallback"], False), "a longer path is not the listed route"
