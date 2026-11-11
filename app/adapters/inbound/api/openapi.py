"""What the OpenAPI document says about the API as a whole: its name, version, description and the groups its routes fall under.

A route describes itself (its models, its docstring); this is the part no single route can say. The document is committed as
`openapi.json` and a test fails while it is stale (`make openapi` rewrites it). Its `servers` list is added by that export and is
deliberately NOT set on the app: Swagger UI at `/docs` calls whatever `servers` names, so a fixed list would send "Try it out" from a page
served behind the front door to a different origin. Served live, the document names no server and a client uses the one it reached.
"""
from typing import Any

from fastapi import FastAPI

API_TITLE = "Graph Agent Core API"

# The CONTRACT's version, not a release number: nothing else in the repository is versioned. Raise it with every change to the contract
# (`make openapi-version` fails one that did not) and its major with a break (`make openapi-breaking` fails a break that did not); which
# step is the author's call. CI's `openapi` job runs both on every pull request.
API_VERSION = "2.0.0"  # 2.0.0: the document was corrected (it said the four streaming routes returned application/json)

API_DESCRIPTION = """\
A multi-tenant, multi-domain LangGraph RAG agent. A client starts a turn, reads its events as they stream, answers an approval the
agent asks for, and lists its own past sessions. One graph serves every domain; `X-Domain` picks which (default `ecorp`).

**Who is calling.** Behind the front door (the authlimit service) a client sends `Authorization: Bearer <access token>`; the door
verifies it and gives the API a signed identity, so the tenant and person are never taken from a header or a body. Only a deployment run
with `AUTH_MODE=headers` (a developer's machine, the tests) reads `X-Tenant-Id` and `X-Principal-Id` as sent. Everything a caller reads or
changes is scoped to its tenant and person.

**Errors.** A refusal the application makes is the canonical envelope `{"code", "message", "details"}`; switch on `code`, not on the
text. A turn's own failures arrive as an `error` event on its stream, in the same shape.

**Streams.** `POST /chat/stream/queued` and `GET /ingest/stream/{job_id}` answer with server-sent events, which OpenAPI describes only
as a `text/event-stream` body; the event vocabulary is in the first route's description.
"""

# Route-level `tags=` name these; the order here is the order the document lists them in.
TAGS: list[dict[str, Any]] = [
    {"name": "chat", "description": "Start a turn, answer the approval it pauses on, or cancel it. A turn runs on an agent worker; these routes stream its events back."},
    {"name": "sessions", "description": "The caller's own past threads and their transcripts, for the given domain."},
    {"name": "usage", "description": "The caller's tenant spend and the limits that apply to it."},
    {"name": "ingest", "description": "Upload documents into the knowledge base. Each file is a job on its own queue, with a progress stream."},
    {"name": "health", "description": "Liveness and readiness. They need no identity."},
    {"name": "web", "description": "The built-in web UI, a single page. Not part of the API a client calls."},
]


def event_streams_as_items(spec: dict[str, Any]) -> None:
    """Rewrite each `text/event-stream` body from `schema` to `itemSchema`, in place.

    The routes answer with a hand-framed StreamingResponse and name an event model under `responses=` (events.py), which FastAPI puts under
    `schema` next to a `type: string` it adds for a non-JSON class: a string that is also one of several objects, which nothing satisfies.
    What the model describes is ONE frame's data, which is what `itemSchema` says (OpenAPI 3.2, and the shape FastAPI's own SSE support
    writes into a 3.1 document): a frame's `data` is a JSON string whose `contentSchema` is the event. oasdiff reads it and reports a change
    inside it ("item schema"), so the breaking-change check sees an event change.
    """
    for path_item in spec.get("paths", {}).values():
        for operation in path_item.values():
            for response in operation.get("responses", {}).values():
                media = response.get("content", {}).get("text/event-stream")
                if media is None or "schema" not in media:
                    continue
                event = {key: value for key, value in media.pop("schema").items() if key not in ("type", "title")}
                media["itemSchema"] = {
                    "type": "object",
                    "required": ["data"],
                    "properties": {"data": {"type": "string", "contentMediaType": "application/json", "contentSchema": event}},
                }


# The routes the front door does not check (class `public` in the door's own route table, which this app may not read: it talks to the door
# over HTTP only, so tests/adapters/inbound/api/test_authlimit_routes_cover_api.py holds this list to the table). Every other operation needs a token.
PUBLIC_OPERATIONS = {("get", "/"), ("get", "/health"), ("get", "/health/ready")}

BEARER = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": (
        "A Casdoor access token, sent to the front door (the authlimit service), which verifies it, charges the rate limit and hands this API a "
        "signed identity. This API never sees the token: the door removes it. A deployment run with `AUTH_MODE=headers` (a developer's "
        "machine, the tests) has no door and takes `X-Tenant-Id` and `X-Principal-Id` as sent instead."
    ),
}


def require_the_front_doors_token(spec: dict[str, Any]) -> None:
    """Declare the bearer scheme, require it everywhere, and say which operations the front door lets through without one, in place.

    This is the contract a client has with the deployed system, not something this process checks (its own check is the signed identity,
    deps.py), so it is written here and not as a FastAPI security dependency, which would parse an Authorization header the door strips.
    """
    spec.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = BEARER
    spec["security"] = [{"bearerAuth": []}]
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if (method, path) in PUBLIC_OPERATIONS:
                operation["security"] = []  # an operation's own list replaces the root's: none needed


def install(app: FastAPI) -> None:
    """Make `app.openapi()` return the document with this module's rewrites applied, once, to the dict FastAPI caches."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        fresh = app.openapi_schema is None
        spec = original()
        if fresh:
            event_streams_as_items(spec)
            require_the_front_doors_token(spec)
        return spec

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI's documented way to extend the document (docs: "Extending OpenAPI")
