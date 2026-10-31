"""The composition root (`app/bootstrap.py`): every port has exactly one production adapter, and each matches its Protocol.

`tests/conftest.py` has already called `wire()` by the time these run, and some autouse fixtures bind fakes over a slot, so the production
adapters are read from `bootstrap.adapters()` rather than from the slots."""
import importlib
import pkgutil

import app.ports
from app import bootstrap
from app.ports.slot import PortNotBound, Slot, all_slots
from tests.fakes.conformance import mismatches


def _import_every_port_module() -> None:
    for module in pkgutil.iter_modules(app.ports.__path__):
        importlib.import_module(f"app.ports.{module.name}")


def test_every_port_has_a_production_adapter_and_no_adapter_is_for_a_port_that_does_not_exist():
    """Adding a port without its entry in `bootstrap.adapters()` (or the reverse) is a failure here, not a PortNotBound in production."""
    _import_every_port_module()

    assert set(all_slots()) == set(bootstrap.adapters())


def test_each_production_adapter_conforms_to_its_protocol():
    _import_every_port_module()

    problems = {
        slot.name: found for slot, adapter in bootstrap.adapters().items() if (found := mismatches(slot.port, adapter))
    }
    assert not problems, problems


def test_the_checker_can_fail():
    """A conformance check that cannot fail proves nothing: a method with the wrong parameters, or a sync one, or a missing one."""

    class Port:
        async def get(self, tenant: str) -> int: ...
        async def put(self, tenant: str, value: int) -> None: ...

    class Wrong:
        async def get(self, tenant_id: str) -> int:
            return 0

        def put(self, tenant: str, value: int) -> None:
            return None

    class Missing:
        pass

    assert [m.split(":")[0] for m in mismatches(Port, Wrong())] == ["get", "put"]
    assert sorted(m.split(":")[0] for m in mismatches(Port, Missing())) == ["get", "put"]


def test_wire_leaves_nothing_unbound():
    _import_every_port_module()
    bootstrap.wire()

    bootstrap.verify_wired()


def test_wire_does_not_replace_an_adapter_that_was_chosen():
    _import_every_port_module()
    slot = next(iter(bootstrap.adapters()))
    chosen = object()
    with slot.overriding(chosen):  # type: ignore[arg-type] - any object stands in for the adapter
        bootstrap.wire()
        assert slot.get() is chosen


def test_verify_wired_names_what_is_missing():
    _import_every_port_module()
    slot: Slot = next(iter(bootstrap.adapters()))
    original = slot.get()
    slot._adapter = None  # the one place a test reaches inside: an unbound slot is the state under test
    try:
        try:
            bootstrap.verify_wired()
        except PortNotBound as exc:
            assert slot.name in str(exc)
        else:
            raise AssertionError("verify_wired accepted an unbound port")
    finally:
        slot._adapter = original
