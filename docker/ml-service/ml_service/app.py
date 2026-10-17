"""The FastAPI app: wiring only. Each endpoint's logic lives in its own module (see the package docstring).

`create_app()` takes optional `loaders` so a test can replace a slot's blocking loader with a fake and exercise the real lifespan,
registry and endpoints without downloading a model.
"""
import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ml_service import classify, embed, guard, health, rerank
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
SUPERVISOR_INTERVAL_S = 1.0
MODELS_TOML = Path(__file__).resolve().parent.parent / "models.toml"


async def _supervise(registry: Registry, interval_s: float) -> None:
    """Reload failed models once their backoff has elapsed. A bounded wait per model (the backoff) inside a loop that only ever
    sleeps one short interval; never raises out (a supervisor that dies would stop all recovery silently)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            retried = await registry.retry_due()
            if retried:
                logger.info("retried failed models", extra={"models": retried})
        except Exception:  # noqa: BLE001 - recovery must keep running whatever one reload does
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
    # A text model is loaded ONCE and serves every role it declares: /v1/embeddings for `embed`, /classify for `classify`.
    text_specs = {s.name: s for s in declared if {"embed", "classify"} & set(s.roles)}
    embed_specs = {name: spec for name, spec in text_specs.items() if "embed" in spec.roles}  # what /v1/embeddings and /v1/models serve
    default_loaders: dict[str, Callable[[], Any]] = {
        "rerank": lambda: rerank.load_rerank_service(settings),
        "prompt-guard": lambda: guard.load_guard_service(settings),
        **{name: partial(embed.load_embed_service, spec, settings) for name, spec in text_specs.items()},
    }
    loaders = {**default_loaders, **(loaders or {})}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
        app.state.classifier = classify.Classifier(
            registry, classifiers, classify.read_heads(settings.router_head_dir, [s.name for s in classifiers]), settings, metrics
        )
        # Load everything concurrently, tolerating failure: a model that cannot load is that model's `failed` state, not the
        # container's. (Before the split either failure stopped the whole service from starting.)
        await registry.load_all()
        supervisor = asyncio.create_task(_supervise(registry, SUPERVISOR_INTERVAL_S))
        try:
            yield
        finally:
            supervisor.cancel()
            await registry.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.embed_specs = embed_specs
    app.include_router(health.router)
    app.include_router(embed.router)
    app.include_router(classify.router)
    app.include_router(rerank.router)
    app.include_router(guard.router)
    return app
