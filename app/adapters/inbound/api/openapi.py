"""What the OpenAPI document says about the API as a whole: its name, version, description and the groups its routes fall under.

A route describes itself (its models, its docstring); this is the part no single route can say. The document is committed as
`openapi.json` and a test fails while it is stale (`make openapi` rewrites it). Its `servers` list is added by that export and is
deliberately NOT set on the app: Swagger UI at `/docs` calls whatever `servers` names, so a fixed list would send "Try it out" from a page
served behind the front door to a different origin. Served live, the document names no server and a client uses the one it reached.
"""
from typing import Any

from fastapi import FastAPI

API_TITLE = "Graph Agent Core API"

# The CONTRACT's version, not a release number: nothing else in the repository is versioned. Bump the minor when a change adds to the
# API and the major when it breaks a client (the CI job `openapi` fails a break against main, see `make openapi-breaking`).
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


def install(app: FastAPI) -> None:
    """Make `app.openapi()` return the document with this module's rewrites applied, once, to the dict FastAPI caches."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        fresh = app.openapi_schema is None
        spec = original()
        if fresh:
            event_streams_as_items(spec)
        return spec

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI's documented way to extend the document (docs: "Extending OpenAPI")
