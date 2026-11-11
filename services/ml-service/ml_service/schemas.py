"""The wire shapes of the ML service, for its OpenAPI document and for nothing else.

No endpoint validates through these models. Each endpoint has its own checks with its own error codes (`error()` in http.py), and the
embeddings response is a large array that must not be validated a second time on the hot path: the routes keep `response_model=None` and
name a model under `responses=` only to describe it. What keeps a model from lying is
tests/services/ml_service/conftest.py, which holds every response any test in that directory gets to the committed document
(services/ml-service/openapi.json), so a field the code sends and the model lacks (or the reverse) fails the test that sent it.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """The service's one error shape (contracts/ml-service.md); `error.code` is the stable part, the message is for a person."""

    error: ErrorBody


# --- /v1/embeddings, /v1/models ----------------------------------------------------------------------------------------------------


class EmbeddingsRequest(BaseModel):
    """The OpenAI embeddings request, as far as this service goes: nothing that truncates (`dimensions`) or batches past its limits."""

    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: None = None


class EmbeddingItem(BaseModel):
    object: Literal["embedding"]
    index: int
    embedding: list[float] | str  # the floats, or little-endian float32 base64 when encoding_format is "base64"


class EmbeddingsUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(BaseModel):
    object: Literal["list"]
    model: str
    data: list[EmbeddingItem]
    usage: EmbeddingsUsage


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"]
    owned_by: str
    state: str
    dim: int | None
    max_length: int
    needs_prefix: dict[str, str]  # documentation for the operator: the server never applies it
    licence: str | None
    revision: str | None


class ModelsResponse(BaseModel):
    object: Literal["list"]
    data: list[ModelInfo]


# --- /v1/decide --------------------------------------------------------------------------------------------------------------------


class ChoiceQuestion(BaseModel):
    type: Literal["choice", "score", "noul"]  # only `choice` is answered; the others are a 422 unsupported_question_type
    instructions: str
    criteria: dict[str, str]  # option -> description; must name exactly the label set's options


class DecideRequest(BaseModel):
    model: Literal["local"] | None = None
    state: str  # the text to decide about
    questions: dict[str, ChoiceQuestion]  # each is named after a label set


class Evidence(BaseModel):
    """Facts, never a decision about them: a caller applies its own policy to the similarity and the margin."""

    nearest: str | None
    similarity: float | None
    runner_up: str | None
    margin: float | None
    truncated: bool


class Answer(BaseModel):
    type: Literal["choice"]
    choice: str | None  # None: nothing is near enough, "none of these"
    x_evidence: Evidence
    x_labelset_version: str | None


class DecideUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class DecideResponse(BaseModel):
    model: str
    answers: dict[str, Answer]
    usage: DecideUsage
    x_served_by: str


# --- /rerank, /prompt-guard --------------------------------------------------------------------------------------------------------


class RerankItem(BaseModel):
    index: int
    score: float  # a raw cross-encoder logit, unbounded; the caller sorts
    text: str


class GuardItem(BaseModel):
    index: int
    label: Literal["BENIGN", "MALICIOUS"]
    malicious_score: float
    text: str


# --- /health, /health/models -------------------------------------------------------------------------------------------------------


class Health(BaseModel):
    status: Literal["ok"]


class ModelState(BaseModel):
    """One model slot. The registry adds what it knows (`dim`, a loaded model's own facts), so more keys than these are normal."""

    model_config = ConfigDict(extra="allow")
    state: str
    roles: list[str]
    licence: str | None = None
    file: str | None = None
    revision: str | None = None
    ready_for_s: float | None = None
    reason: str | None = None
    retry_in_s: float | None = None


class LabelsetState(BaseModel):
    """A label set: its version and labels and, per model, whether its index is loaded; or why the set was refused."""

    state: str | None = None
    version: str | None = None
    labels: list[str] | None = None
    models: dict[str, str] | None = None


class HealthModels(BaseModel):
    status: Literal["ok", "degraded"]
    models: dict[str, ModelState]
    labelsets: dict[str, LabelsetState]


# --- what the routes pass to FastAPI ------------------------------------------------------------------------------------------------

_RETRY_AFTER = {"Retry-After": {"description": "Whole seconds until the model is worth asking again (its backoff left).", "schema": {"type": "integer"}}}
_ERRORS = {
    400: "The request is malformed or past a documented limit; `error.code` says which.",
    404: "`model_not_found`: no such model is served here.",
    413: "`input_too_long`: the text is past the limit.",
    422: "A field or question type this service does not answer (`unsupported_*`).",
    500: "`internal`: the model failed on this request. It never takes the service down.",
    503: "`model_unavailable`: the model is loading, or failed and is waiting out its backoff.",
}


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries for the error statuses a route can answer, each with the one error body (503 also names Retry-After)."""
    return {s: {"description": _ERRORS[s], "model": ErrorResponse, **({"headers": _RETRY_AFTER} if s == 503 else {})} for s in statuses}


def request_body(model: type[BaseModel]) -> dict[str, Any]:
    """`openapi_extra=` for a route that reads its own JSON (so its own error codes decide what a bad body earns, not FastAPI's 422): the
    model's schema, with its `$defs` inlined because a schema reached only through `openapi_extra` is not added to `components`."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[1]])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return {"requestBody": {"required": True, "content": {"application/json": {"schema": inline(schema)}}}}
