"""`app/ports/slot.py`: the seam the application reaches its adapters through."""
import pytest

from app.ports.slot import PortNotBound, Slot, all_slots, unbound


class _Port:
    async def ping(self) -> str: ...


class _A:
    async def ping(self) -> str:
        return "a"


class _B(_A):
    async def ping(self) -> str:
        return "b"


@pytest.fixture
def slot():
    return Slot("test_only_port", _Port, register=False)  # the registry is process-wide: this one is not the app's


def test_an_unbound_slot_fails_closed_and_names_the_port(slot):
    with pytest.raises(PortNotBound, match="test_only_port"):
        slot.get()
    assert slot.bound is False
    assert "test_only_port" not in unbound() and slot not in all_slots()


def test_a_registered_slot_is_listed_until_it_is_bound():
    registered = Slot("test_only_listed_port", _Port)
    try:
        assert "test_only_listed_port" in unbound()
        registered.provide(_A())
        assert "test_only_listed_port" not in unbound()
    finally:
        from app.ports import slot as slot_module

        slot_module._ALL.remove(registered)


def test_provide_binds_once_and_never_replaces(slot):
    slot.provide(_A())
    first = slot.get()
    slot.provide(_B())

    assert slot.get() is first, "a second wire() must not undo an adapter someone chose"


def test_bind_replaces(slot):
    slot.provide(_A())
    replacement = _B()
    slot.bind(replacement)

    assert slot.get() is replacement


def test_overriding_restores_what_was_there_even_when_the_block_raises(slot):
    original = _A()
    slot.provide(original)

    with pytest.raises(RuntimeError), slot.overriding(_B()) as fake:
        assert slot.get() is fake
        raise RuntimeError("the test failed")

    assert slot.get() is original


def test_overriding_an_unbound_slot_leaves_it_unbound_afterwards(slot):
    with slot.overriding(_A()):
        assert slot.bound
    assert not slot.bound


async def test_the_adapter_is_asked_at_call_time(slot):
    """The application reads the slot on every call, so a binding made after an import still takes effect."""
    slot.provide(_A())
    assert await slot.get().ping() == "a"
    with slot.overriding(_B()):
        assert await slot.get().ping() == "b"
