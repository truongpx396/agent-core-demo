"""A named seam between the application and one adapter: the registry every port hangs off.

The application (`app/agent`, `app/domains`) depends on a port, a `Protocol` in `app/ports/`, and asks its `Slot` for the
implementation at the moment it needs one. Which implementation that is gets decided once per process, at the composition
root (`app/bootstrap.py`), by binding an adapter to the slot. Nothing in the application names an adapter, so the
dependency rule (`ARCHITECTURE.md`) can be checked by reading imports.

Why a slot and not an argument threaded through every call: the code this was retrofitted onto is module-level functions
called from graph nodes, tools and CLIs, and the repo already has this idiom for its one earlier port
(`billing_port.billing`: "a module-level name read at CALL time, the repo's idiom for a seam a test replaces"). A slot is that
idiom with a type, a name in the error and a way to list every seam. The graph's own clients keep their constructor
injection (`GraphDeps`); a slot is for what a plain function has to reach.

An unbound slot FAILS CLOSED with `PortNotBound`, naming the port. There is no default adapter: a default would put the
application back in the business of knowing which adapter is the real one. Production entrypoints call
`app.bootstrap.wire()` first and `verify_wired()` makes a missing one a startup error instead of the first request's;
`tests/conftest.py` wires the same adapters (whose I/O the suite already guards) so existing tests run unchanged, and a test of
a consumer binds a fake with `Slot.overriding`.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Generic, TypeVar

T = TypeVar("T")

_ALL: list[Slot] = []


class PortNotBound(RuntimeError):
    """A port was used before any adapter was bound to it: the process did not call `app.bootstrap.wire()`."""


class Slot(Generic[T]):
    """One port's binding. Create one per port, at module level, next to the Protocol it holds. `port` is that Protocol: the conformance
    tests (`tests/fakes/conformance.py`) read it to check an adapter, or a fake, against the contract it claims to implement."""

    def __init__(self, name: str, port: type, *, register: bool = True) -> None:
        self.name = name
        self.port = port
        self._adapter: T | None = None
        if register:  # a slot a test makes for itself must not show up in the app's list of ports
            _ALL.append(self)

    def provide(self, adapter: T) -> None:
        """Installs `adapter` unless one is already bound. What the composition root uses: calling `wire()` again, or from a second entrypoint
        helper, never replaces an adapter someone chose on purpose (a test's fake, an operator job's own client)."""
        if self._adapter is None:
            self._adapter = adapter

    def bind(self, adapter: T) -> None:
        """Installs `adapter`, replacing any earlier one. For a process that has to change its mind; the application never calls it."""
        self._adapter = adapter

    def get(self) -> T:
        if self._adapter is None:
            raise PortNotBound(
                f"port {self.name!r} has no adapter bound: the process entrypoint must call app.bootstrap.wire() before it runs"
            )
        return self._adapter

    @property
    def bound(self) -> bool:
        return self._adapter is not None

    @contextmanager
    def overriding(self, adapter: T) -> Iterator[T]:
        """Binds `adapter` for the duration of a `with` block and restores what was there. For tests."""
        previous = self._adapter
        self._adapter = adapter
        try:
            yield adapter
        finally:
            self._adapter = previous


def all_slots() -> list[Slot]:
    """Every slot created so far (a port module creates its slots when imported)."""
    return list(_ALL)


def unbound() -> list[str]:
    return [slot.name for slot in _ALL if not slot.bound]
