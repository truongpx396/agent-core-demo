"""The FastAPI app: wiring only. Each endpoint's logic lives in its own module (see the package docstring).

`create_app()` takes optional `loaders` so a test can replace a slot's blocking loader with a fake and exercise the real lifespan,
registry and endpoints without downloading a model.
"""
import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ml_service import decide, embed, guard, health, labelsets, rerank
from ml_service.metrics import Metrics
from ml_service.registry import (
    ModelSpec,
    Registry,
    Slot,
    enabled_specs,
    load_model_specs,
)
from ml_service.settings import Settings

logger = logging.getLogger("ml_service")

# What the OpenAPI document says about the service as a whole (the routes describe themselves). Committed as
# `services/ml-service/openapi.json`; a test fails while it is stale and `make openapi` rewrites it. The version is the CONTRACT's, not
# a release number: bump the minor when a change adds to the API, the major when it breaks a caller (CI's `openapi` job checks).
API_TITLE = "Graph Agent Core ML Service"
API_VERSION = "1.0.0"
API_DESCRIPTION = """\
The models the agent runs next to it, on CPU, behind one internal HTTP API: sentence embeddings, a reranker, a prompt-injection
screen, and typed decisions (pick one label out of a served label set, or "none of these"). The app's `mlservice` adapter is the
caller. It is an internal service with no authentication of its own, so it is never published; reach it only over the private network.

A model that fails to load is that model's `failed` state, not the container's: `/health` stays 200 so Docker does not restart a
process that is only missing a model, and `/health/models` says which one is down and when it is retried.
"""
API_TAGS = [
    {"name": "embeddings", "description": "OpenAI-compatible `/v1/embeddings`, and the models it serves (`/v1/models`)."},
    {"name": "decide", "description": "A typed decision: which label of a served label set fits the text, or none."},
    {"name": "rerank", "description": "Order passages by relevance to a query."},
    {"name": "prompt-guard", "description": "Screen a text for prompt injection."},
    {"name": "operations", "description": "Liveness, per-model state and Prometheus metrics."},
]
SUPERVISOR_INTERVAL_S = 1.0
MODELS_TOML = Path(__file__).resolve().parent.parent / "models.toml"


async def _supervise(registry: Registry, interval_s: float, labeller: labelsets.Labeller | None = None) -> None:
    """Reload failed models once their backoff has elapsed, then re-index the label sets for any model that came back. A bounded wait per
    model (the backoff) inside a loop that only ever sleeps one short interval; never raises out (a supervisor that dies would stop all
    recovery silently)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            retried = await registry.retry_due()
            if retried:
                logger.info("retried failed models", extra={"models": retried})
            if labeller is not None:
                await labeller.sync()
        except Exception:  # recovery must keep running whatever one reload does
            logger.warning("model retry tick failed", exc_info=True)


async def _start(value: Any) -> None:
    await value.start()


async def _stop(value: Any) -> None:
    await value.stop()


def create_app(
    settings: Settings | None = None,
    *,
    loaders: dict[str, Callable[[], Any]] | None = None,
    specs: list[ModelSpec] | None = None,
) -> FastAPI:
    """`loaders` replaces a slot's blocking loader by name and `specs` replaces models.toml, so a test drives the real lifespan,
    registry and endpoints with fakes and no download."""
    settings = settings or Settings.from_env()
    metrics = Metrics()
    declared = enabled_specs(load_model_specs(MODELS_TOML) if specs is None else specs, settings.models_enabled)
    # A text model is loaded ONCE and serves every role it declares: /v1/embeddings for `embed`, /v1/decide for `classify`.
    text_specs = {s.name: s for s in declared if {"embed", "classify"} & set(s.roles)}
    embed_specs = {name: spec for name, spec in text_specs.items() if "embed" in spec.roles}  # what /v1/embeddings and /v1/models serve
    default_loaders: dict[str, Callable[[], Any]] = {
        "rerank": lambda: rerank.load_rerank_service(settings),
        "prompt-guard": lambda: guard.load_guard_service(settings),
        **{name: partial(embed.load_embed_service, spec, settings) for name, spec in text_specs.items()},
    }
    loaders = {**default_loaders, **(loaders or {})}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry = Registry(
            base_backoff_s=settings.model_retry_base_s,
            max_backoff_s=settings.model_retry_max_s,
            on_state=metrics.set_model_state,
        )
        for name, roles in (("rerank", ("rerank",)), ("prompt-guard", ("screen",))):
            registry.add(Slot(name=name, roles=roles, loader=loaders[name], activate=_start, deactivate=_stop))
        for name, spec in text_specs.items():
            info = {"licence": spec.licence, "file": spec.file, "revision": spec.revision}  # replaced by the loaded model's own facts
            registry.add(Slot(name=name, roles=spec.roles, loader=loaders[name], info=info, activate=_start, deactivate=_stop))
        app.state.registry = registry
        classifiers = [spec for spec in text_specs.values() if "classify" in spec.roles]
        app.state.labeller = labelsets.Labeller(
            registry, classifiers, labelsets.read_labelsets(settings.labelset_dir), settings, metrics
        )
        # Load everything concurrently, tolerating failure: a model that cannot load is that model's `failed` state, not the
        # container's. (Before the split either failure stopped the whole service from starting.)
        await registry.load_all()
        await app.state.labeller.sync()  # so the first label request after start finds its index, not "pending"
        supervisor = asyncio.create_task(_supervise(registry, SUPERVISOR_INTERVAL_S, app.state.labeller))
        try:
            yield
        finally:
            supervisor.cancel()
            await registry.close()

    app = FastAPI(title=API_TITLE, version=API_VERSION, description=API_DESCRIPTION, openapi_tags=API_TAGS, lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.embed_specs = embed_specs
    app.include_router(health.router)
    app.include_router(embed.router)
    app.include_router(decide.router)
    app.include_router(rerank.router)
    app.include_router(guard.router)
    return app
