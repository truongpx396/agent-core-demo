"""Liveness, readiness and the built-in web UI — the endpoints that need no caller identity."""
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from app.api import (
    health as health_checks,  # `health` is also this module's own liveness endpoint name below
)
from app.api.schemas import HealthResponse, ReadinessResponse

router = APIRouter()

_UI_HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
def ui() -> str:
    """The built-in web UI (pattern 29) — a single self-contained page, no
    build step, no CDN dependency. Talks only to `POST /chat/stream/queued`'s
    SSE vocabulary, never a special-cased endpoint of its own. Read from
    disk per request (not cached) so editing the file and refreshing is
    enough during development — this isn't a hot path.
    """
    return _UI_HTML_PATH.read_text()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness only — always 200 if this process can respond at all. See
    `GET /health/ready` for whether it can actually complete a turn (see
    app/api/health.py on why these stay separate endpoints)."""
    return HealthResponse()


@router.get("/health/ready", response_model=ReadinessResponse)
async def health_ready(response: Response) -> ReadinessResponse:
    """Readiness — 200 only if every real dependency is reachable right now,
    503 otherwise, with which one(s) failed in the body (see
    app/api/health.py::check_dependencies)."""
    checks = await health_checks.check_dependencies()
    ready = all(checks.values())
    response.status_code = 200 if ready else 503
    return ReadinessResponse(status="ready" if ready else "degraded", checks=checks)
