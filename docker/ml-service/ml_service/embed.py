"""POST /v1/embeddings (OpenAI-compatible) and GET /v1/models (specs/014 FR-021..FR-026, contracts/ml-service.md).

WHY OPENAI-COMPATIBLE. The app reaches every model through the LiteLLM proxy by alias. LiteLLM can forward the `embed` alias to any
OpenAI-compatible endpoint, so serving this shape from the ML service means switching the app to an ONNX model served here is a gateway
CONFIGURATION change with no application code change (contracts/gateway-aliases.md).

THE SERVER NEVER ADDS A PREFIX. The request carries no notion of "this is a question" versus "this is a passage", so the server cannot
know which prefix a text needs; the app prepends it (EMBED_QUERY_PREFIX / EMBED_DOCUMENT_PREFIX). `/v1/models` echoes what each model
expects as documentation for the operator. A server-side default would silently double-prefix.

REQUESTS ARE BOUNDED, NOT TRUNCATED. More than `ML_EMBED_MAX_ITEMS` inputs, or any input over `ML_EMBED_MAX_CHARS` characters, is
rejected with a 400 naming the problem: a caller must be told its input did not fit. The model's own context limit is different and
unavoidable: text longer than `max_length` tokens is cut by the tokenizer, and the response says how many items were
(`X-Truncated-Items`), as the guard has always done.
"""
import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ml_service.batching import MicroBatcher
from ml_service.http import error, unavailable
from ml_service.registry import ModelSpec
from ml_service.settings import Settings
from ml_service.text_model import TextModel, load_text_model


@dataclass(frozen=True)
class Embedded:
    vector: np.ndarray
    tokens: int  # tokens actually fed after truncation: what `usage` reports
    truncated: bool


class EmbedService:
    """A loaded text model plus the micro-batcher that lets concurrent requests share model passes."""

    def __init__(self, model: TextModel, *, max_batch_size: int, max_wait_ms: float) -> None:
        self.model = model
        self._batcher: MicroBatcher[str, Embedded] = MicroBatcher(
            self._embed_batch, max_batch_size=max_batch_size, max_wait_ms=max_wait_ms
        )

    def _embed_batch(self, texts: list[str]) -> list[Embedded]:
        encoded = self.model.text.embed(texts)
        return [Embedded(encoded.vectors[i], encoded.token_counts[i], encoded.truncated_items[i]) for i in range(len(texts))]

    def info(self) -> dict:
        return self.model.info()

    async def start(self) -> None:
        self._batcher.start()

    async def stop(self) -> None:
        self._batcher.stop()

    async def embed(self, texts: list[str]) -> list[Embedded]:
        return list(await asyncio.gather(*(self._batcher.submit(t) for t in texts)))


def load_embed_service(spec: ModelSpec, settings: Settings) -> EmbedService:
    return EmbedService(
        load_text_model(spec, settings), max_batch_size=settings.embed_batch_size, max_wait_ms=settings.max_batch_wait_ms
    )


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def _validate(body: Any, specs: dict[str, ModelSpec], settings: Settings) -> tuple[ModelSpec, list[str], str]:
    """The request's (model, inputs, encoding), or the rejection it earns. The order is the contract's: a malformed body, then an
    unknown model, then the parameters, then the input and its bounds."""
    if not isinstance(body, dict):
        raise _Reject(400, "invalid_input", "the request body must be a JSON object")
    name = body.get("model")
    if not isinstance(name, str) or not name:
        raise _Reject(400, "invalid_input", "`model` is required")
    spec = specs.get(name)
    if spec is None:
        raise _Reject(404, "model_not_found", f"model {name!r} is not served here; served models: {sorted(specs) or 'none'}")
    if body.get("dimensions") is not None:
        raise _Reject(400, "dimensions_unsupported", "this service does not truncate embeddings; `dimensions` is not supported")
    encoding = body.get("encoding_format") or "float"
    if encoding not in ("float", "base64"):
        raise _Reject(400, "invalid_input", "`encoding_format` must be \"float\" or \"base64\"")
    raw = body.get("input")
    inputs = [raw] if isinstance(raw, str) else raw
    if not isinstance(inputs, list) or not inputs or not all(isinstance(t, str) and t for t in inputs):
        raise _Reject(400, "invalid_input", "`input` must be a non-empty string or a non-empty list of non-empty strings")
    if len(inputs) > settings.embed_max_items:
        raise _Reject(400, "batch_too_large", f"{len(inputs)} inputs exceeds the limit of {settings.embed_max_items}; split the request")
    for index, text in enumerate(inputs):
        if len(text) > settings.embed_max_chars:
            raise _Reject(400, "input_too_long", f"input {index} has {len(text)} characters; the limit is {settings.embed_max_chars}")
    return spec, inputs, encoding


def _encode(vector: np.ndarray, encoding: str) -> Any:
    if encoding == "base64":
        return base64.b64encode(np.asarray(vector, dtype="<f4").tobytes()).decode("ascii")
    return [float(x) for x in vector]


router = APIRouter()


@router.post("/v1/embeddings", response_model=None)
async def create_embeddings(request: Request):
    app = request.app
    settings: Settings = app.state.settings
    metrics = app.state.metrics
    started = time.perf_counter()
    try:
        body = await request.json()
    except ValueError:
        return error(400, "invalid_input", "the request body is not valid JSON")
    try:
        spec, inputs, encoding = _validate(body, app.state.embed_specs, settings)
    except _Reject as rejected:
        metrics.embed_errors.labels(model=str(body.get("model", "unknown")) if isinstance(body, dict) else "unknown", code=rejected.code).inc()
        return error(rejected.status, rejected.code, rejected.message)

    service: EmbedService | None = app.state.registry.ready_value(spec.name)
    if service is None:
        metrics.embed_errors.labels(model=spec.name, code="model_unavailable").inc()
        return unavailable(request, spec.name)

    metrics.embed_requests.labels(model=spec.name).inc()
    try:
        results = await service.embed(inputs)
    except Exception:  # noqa: BLE001 - a model failure is this request's 500, never the service's; the class is counted, no input is logged
        metrics.embed_errors.labels(model=spec.name, code="internal").inc()
        return error(500, "internal", "the model failed to embed this request")

    metrics.embed_items.labels(model=spec.name).inc(len(inputs))
    metrics.embed_batch.labels(model=spec.name).observe(len(inputs))
    metrics.embed_latency.labels(model=spec.name).observe(time.perf_counter() - started)
    tokens = sum(r.tokens for r in results)
    payload = {
        "object": "list",
        "model": spec.name,
        "data": [{"object": "embedding", "index": i, "embedding": _encode(r.vector, encoding)} for i, r in enumerate(results)],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
    return JSONResponse(payload, headers={"X-Truncated-Items": str(sum(1 for r in results if r.truncated))})


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    registry = request.app.state.registry
    snapshot = registry.snapshot()
    data = []
    for name, spec in request.app.state.embed_specs.items():
        item = snapshot.get(name, {})
        data.append(
            {
                "id": name,
                "object": "model",
                "owned_by": "ml-service",
                "state": item.get("state", "not_loaded"),
                "dim": item.get("dim"),
                "max_length": spec.max_length,
                "needs_prefix": spec.needs_prefix,  # documentation for the operator: the server never applies it
                "licence": spec.licence,
                "revision": spec.revision,
            }
        )
    return {"object": "list", "data": data}
