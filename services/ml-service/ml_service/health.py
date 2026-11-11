"""GET /health (liveness, unchanged), GET /health/models (per-model state, new) and GET /metrics (new).

`/health` stays "the process answers": a model that failed to load must not make Docker restart the container (FR-024). The detail,
including which model is down and when it will be retried, is `/health/models`; the app's readiness check reads it and reports a
degraded model as a warning, never as an unready API.
"""
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from ml_service.schemas import Health, HealthModels

router = APIRouter(tags=["operations"])


@router.get("/health", response_model=None, responses={200: {"description": "The process answers. It says nothing of the models.", "model": Health}})
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/health/models", response_model=None, responses={200: {"description": "Per-model state and the label sets' index state.", "model": HealthModels}})
async def health_models(request: Request) -> dict[str, Any]:
    registry = request.app.state.registry
    return {
        "status": "degraded" if registry.degraded else "ok",
        "models": registry.snapshot(),
        "labelsets": request.app.state.labeller.snapshot(),
    }


@router.get("/metrics", response_class=PlainTextResponse, responses={200: {"description": "Prometheus text exposition format."}})
async def metrics(request: Request) -> Response:
    body, content_type = request.app.state.metrics.render()
    return Response(content=body, media_type=content_type)
