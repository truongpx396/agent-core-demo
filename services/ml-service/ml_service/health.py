"""GET /health (liveness, unchanged), GET /health/models (per-model state, new) and GET /metrics (new).

`/health` stays "the process answers": a model that failed to load must not make Docker restart the container (FR-024). The detail,
including which model is down and when it will be retried, is `/health/models`; the app's readiness check reads it and reports a
degraded model as a warning, never as an unready API.
"""
from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/models")
async def health_models(request: Request) -> dict:
    registry = request.app.state.registry
    return {
        "status": "degraded" if registry.degraded else "ok",
        "models": registry.snapshot(),
        "labelsets": request.app.state.labeller.snapshot(),
    }


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    body, content_type = request.app.state.metrics.render()
    return Response(content=body, media_type=content_type)
