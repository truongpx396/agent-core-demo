"""Fakes for the ML service's hermetic tests: two text models whose vectors a test chooses, and the real app built around them.

The real app, lifespan, registry and supervisor are used; only a model's `embed` is replaced, because what the tests are about is the routing
between models, the label sets and the endpoints, not the ONNX pass.
"""
import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from ml_service.app import create_app
from ml_service.embed import Embedded
from ml_service.registry import ModelSpec
from ml_service.settings import Settings
from tests.ml_service.test_rerank_guard_contract import _guard_loader, _rerank_loader

PRIMARY, FALLBACK = "primary-m", "fallback-m"
REVISIONS = {PRIMARY: "a" * 40, FALLBACK: "b" * 40}


def spec(name: str, *, roles=("embed", "classify")) -> ModelSpec:
    return ModelSpec(name=name, roles=roles, repo=f"org/{name}", file="onnx/model.onnx", revision=REVISIONS.get(name, "c" * 40),
                     pooling="cls", normalize=True, max_length=8, pad_token="[PAD]", licence="MIT")


def settings(**overrides) -> Settings:
    base = dict(rerank_model_name="x", guard_model_repo="x", rerank_max_batch_size=8, guard_max_batch_size=8, max_batch_wait_ms=2,
                onnx_intra_op_threads=1, model_retry_base_s=0.02, model_retry_max_s=0.05, models_enabled=(),
                classify_deadline_ms=60.0, classify_max_chars=40)
    return Settings(**{**base, **overrides})


class FakeText:
    """Stands in for an `EmbedService`: the same surface the matcher uses (`model`, `embed`, `info`, `start`, `stop`). Any text gets [3, 4]."""

    def __init__(self, name: str, *, revision: str | None = None, dim: int = 2, mode: str = "ok", truncated: bool = False) -> None:
        self.model = SimpleNamespace(spec=spec(name), revision=revision or REVISIONS[name], dim=dim)
        self.mode = mode
        self.truncated = truncated
        self.calls: list[list[str]] = []

    def info(self) -> dict:
        return {"revision": self.model.revision, "dim": self.model.dim}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def embed(self, texts: list[str]) -> list[Embedded]:
        self.calls.append(list(texts))
        if self.mode == "raise":
            raise RuntimeError("the session blew up")
        if self.mode == "hang":
            await asyncio.Event().wait()  # never completes: the caller's deadline is what ends it
        if self.mode == "nan":
            return [Embedded(np.array([np.nan, np.nan], dtype=np.float32), 3, False) for _ in texts]
        return [Embedded(np.array([3.0, 4.0], dtype=np.float32), 3, self.truncated) for _ in texts]


def loaders_for(services: dict[str, Any]) -> dict[str, Callable[[], Any]]:
    return {"rerank": _rerank_loader, "prompt-guard": _guard_loader, **{name: (lambda s=s: s) for name, s in services.items()}}


def make_client(*, services: dict[str, Any] | None = None, loaders: dict[str, Any] | None = None, **settings_kw):
    services = services or {PRIMARY: FakeText(PRIMARY), FALLBACK: FakeText(FALLBACK)}
    all_loaders = {**loaders_for(services), **(loaders or {})}
    app = create_app(settings(**settings_kw), specs=[spec(PRIMARY), spec(FALLBACK)], loaders=all_loaders)
    return TestClient(app), services
