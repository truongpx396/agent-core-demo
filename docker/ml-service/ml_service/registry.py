"""Declared models and the load state of every model this service serves (data-model.md section 8, FR-024).

Before the package split the service loaded both of its models in `lifespan`, so if either failed to load the whole container failed
to start and took the other endpoint down with it. Now each model is a `Slot` with its own state:

    not_loaded -> loading -> ready
                  loading -> failed(reason) -> (after a backoff) loading
                  ready   -> failed(reason)   on a run error

A failure is retried after 5 s, 10 s, 20 s ... capped at `max_backoff_s` (default 300 s): a wait that can only end when a counterparty
acts must have a ceiling (Principle V), and a model whose file is missing must not be re-downloaded on every request. One slot's state
never affects another's. The clock is injected so the whole machine is tested without sleeping.
"""
import asyncio
import re
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ml_service.onnx_text import POOLINGS

ROLES = ("embed", "classify")
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class State(str, Enum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelSpec:
    """One entry of docker/ml-service/models.toml."""

    name: str
    roles: tuple[str, ...]
    repo: str
    file: str
    revision: str  # a full 40-hex commit sha, never a branch
    pooling: str
    normalize: bool
    max_length: int
    pad_token: str
    licence: str
    output: str | None = None
    needs_prefix: dict[str, str] = field(default_factory=dict)  # documentation only: the server NEVER applies a prefix
    enabled: bool = True


def parse_model_specs(text: str) -> list[ModelSpec]:
    """Parse and validate a models.toml. Every rule here is a refusal at load time, not a surprise at request time."""
    raw = tomllib.loads(text).get("model", [])
    specs: list[ModelSpec] = []
    seen: set[str] = set()
    for entry in raw:
        where = f"model {entry.get('name', '?')!r}"
        for required in ("name", "roles", "repo", "file", "revision", "pooling", "max_length", "pad_token", "licence"):
            if required not in entry:
                raise ValueError(f"{where}: missing required field {required!r}")
        name = str(entry["name"])
        if not _NAME.match(name):
            raise ValueError(f"{where}: name must match {_NAME.pattern}")
        if name in seen:
            raise ValueError(f"{where}: duplicate name")
        seen.add(name)
        if not str(entry["licence"]).strip():
            raise ValueError(f"{where}: licence must not be empty")
        if not _SHA.match(str(entry["revision"])):
            raise ValueError(f"{where}: revision must be a full 40-hex commit sha, not a branch or a short sha")
        roles = tuple(entry["roles"])
        if not roles or any(r not in ROLES for r in roles):
            raise ValueError(f"{where}: roles must be a non-empty subset of {ROLES}")
        if entry["pooling"] not in POOLINGS:
            raise ValueError(f"{where}: pooling must be one of {POOLINGS}")
        if int(entry["max_length"]) < 1:
            raise ValueError(f"{where}: max_length must be positive")
        specs.append(
            ModelSpec(
                name=name,
                roles=roles,
                repo=str(entry["repo"]),
                file=str(entry["file"]),
                revision=str(entry["revision"]),
                pooling=str(entry["pooling"]),
                normalize=bool(entry.get("normalize", True)),
                max_length=int(entry["max_length"]),
                pad_token=str(entry["pad_token"]),
                licence=str(entry["licence"]),
                output=entry.get("output"),
                needs_prefix={k: str(v) for k, v in (entry.get("needs_prefix") or {}).items()},
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return specs


def load_model_specs(path: Path) -> list[ModelSpec]:
    return parse_model_specs(path.read_text()) if path.exists() else []


def enabled_specs(specs: list[ModelSpec], override: tuple[str, ...]) -> list[ModelSpec]:
    """`ML_MODELS_ENABLED` wins over the registry's own `enabled` flags; naming an unknown model is a configuration error."""
    if not override:
        return [s for s in specs if s.enabled]
    known = {s.name for s in specs}
    unknown = [n for n in override if n not in known]
    if unknown:
        raise ValueError(f"ML_MODELS_ENABLED names models the registry does not declare: {unknown}")
    return [s for s in specs if s.name in override]


@dataclass
class Slot:
    name: str
    roles: tuple[str, ...]
    loader: Callable[[], Any]  # blocking; runs in a worker thread
    info: dict[str, Any] = field(default_factory=dict)  # revision, dim, licence ... reported by /health/models
    activate: Callable[[Any], Awaitable[None]] | None = None  # runs in the loop once loaded (starts a batcher)
    deactivate: Callable[[Any], Awaitable[None]] | None = None
    state: State = State.NOT_LOADED
    value: Any = None
    reason: str | None = None
    since: float | None = None
    failures: int = 0
    retry_at: float | None = None


class Registry:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        base_backoff_s: float = 5.0,
        max_backoff_s: float = 300.0,
        on_state: Callable[[str, State], None] | None = None,
    ) -> None:
        self._clock = clock
        self._base = base_backoff_s
        self._max = max_backoff_s
        self._on_state = on_state
        self._slots: dict[str, Slot] = {}

    def add(self, slot: Slot) -> None:
        if slot.name in self._slots:
            raise ValueError(f"duplicate slot {slot.name!r}")
        self._slots[slot.name] = slot
        self._set(slot, State.NOT_LOADED)

    def slot(self, name: str) -> Slot | None:
        return self._slots.get(name)

    def names(self) -> list[str]:
        return list(self._slots)

    def ready_value(self, name: str) -> Any | None:
        slot = self._slots.get(name)
        return slot.value if slot is not None and slot.state is State.READY else None

    def backoff_s(self, failures: int) -> float:
        """5, 10, 20 ... capped. `failures` counts consecutive failures including the one just recorded."""
        return min(self._base * (2 ** max(failures - 1, 0)), self._max)

    def _set(self, slot: Slot, state: State) -> None:
        slot.state = state
        if state is State.READY:
            slot.since = self._clock()
        if self._on_state is not None:
            self._on_state(slot.name, state)

    async def load(self, name: str) -> bool:
        """Load one slot. Single-flight: a slot already loading is left alone. Returns True when it ended `ready`."""
        slot = self._slots[name]
        if slot.state is State.LOADING:
            return False
        self._set(slot, State.LOADING)
        try:
            value = await asyncio.to_thread(slot.loader)
            if slot.activate is not None:
                await slot.activate(value)
        except Exception as exc:  # noqa: BLE001 - any load failure is this slot's state, never the whole service's
            self._fail(slot, f"{type(exc).__name__}: {exc}")
            return False
        slot.value = value
        slot.reason = None
        slot.failures = 0
        slot.retry_at = None
        self._set(slot, State.READY)
        return True

    def _fail(self, slot: Slot, reason: str) -> None:
        slot.failures += 1
        slot.reason = reason[:300]
        slot.retry_at = self._clock() + self.backoff_s(slot.failures)
        self._set(slot, State.FAILED)

    def fail(self, name: str, reason: str) -> None:
        """A run error on a `ready` slot: it becomes `failed` and is reloaded after the backoff."""
        slot = self._slots[name]
        if slot.state is State.READY:
            self._fail(slot, reason)

    async def load_all(self) -> None:
        await asyncio.gather(*(self.load(n) for n in self._slots))

    async def retry_due(self) -> list[str]:
        """Reload every failed slot whose backoff has elapsed. Called by the supervisor tick; returns the names retried."""
        now = self._clock()
        due = [s.name for s in self._slots.values() if s.state is State.FAILED and s.retry_at is not None and s.retry_at <= now]
        if due:
            await asyncio.gather(*(self.load(n) for n in due))
        return due

    def retry_after_s(self, name: str, *, default: int = 5) -> int:
        """Whole seconds a caller should wait before asking again: the backoff left on a failed model, else a short default."""
        slot = self._slots.get(name)
        if slot is not None and slot.state is State.FAILED and slot.retry_at is not None:
            return max(1, int(slot.retry_at - self._clock()))
        return default

    async def close(self) -> None:
        for slot in self._slots.values():
            if slot.deactivate is not None and slot.value is not None:
                await slot.deactivate(slot.value)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        out: dict[str, dict[str, Any]] = {}
        for slot in self._slots.values():
            item: dict[str, Any] = {"state": slot.state.value, "roles": list(slot.roles), **slot.info}
            if slot.state is State.READY and slot.since is not None:
                item["ready_for_s"] = round(now - slot.since, 1)
            if slot.state is State.FAILED:
                item["reason"] = slot.reason
                item["retry_in_s"] = round(max(0.0, (slot.retry_at or now) - now), 1)
            out[slot.name] = item
        return out

    @property
    def degraded(self) -> bool:
        return any(s.state is not State.READY for s in self._slots.values())
