"""The service's one error shape: `{"error": {"code": str, "message": str}}` with the HTTP status (contracts/ml-service.md)."""
from fastapi import Request
from fastapi.responses import JSONResponse


def error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def unavailable(request: Request, name: str) -> JSONResponse:
    """503 for a model that is not `ready` (still loading, or failed and waiting out its backoff). `Retry-After` is the backoff left,
    so a caller neither hammers a failed model nor waits longer than the service will."""
    retry_after = request.app.state.registry.retry_after_s(name)
    return error(503, "model_unavailable", f"model {name!r} is not ready", {"Retry-After": str(retry_after)})
