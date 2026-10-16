"""`tests/ids.py::hex_id` never returns an id that contains a string a test plants in a payload."""
import uuid

from tests import ids


def test_an_id_that_happens_to_contain_a_planted_string_is_not_used(monkeypatch):
    draws = iter([
        uuid.UUID("4242abcdef0123456789abcdef012345"),  # starts with the card's last four digits
        uuid.UUID("0a14242bcd0123456789abcdef012345"),  # has them inside the ten characters that would be used
        uuid.UUID("a1b2c3d4e5f60718293a4b5c6d7e8f90"),
    ])
    monkeypatch.setattr(ids.uuid, "uuid4", lambda: next(draws))

    assert ids.hex_id(10) == "a1b2c3d4e5"
    assert next(draws, None) is None, "all three draws were consumed: the first two were rejected"


def test_only_the_characters_actually_used_are_checked(monkeypatch):
    """A planted string further along the uuid is not in the id, so rejecting it would only waste a draw."""
    monkeypatch.setattr(ids.uuid, "uuid4", lambda: uuid.UUID("0123456789abcdef4242abcdef012345"))

    assert ids.hex_id(10) == "0123456789"


def test_the_ids_it_returns_have_the_requested_length_and_are_hex():
    for length in (8, 10):
        value = ids.hex_id(length)
        assert len(value) == length
        int(value, 16)


def test_the_webhook_tests_world_never_hands_out_an_id_with_the_planted_card_digits(monkeypatch):
    """The failure this exists for: `TestWhatIsStored` searches the stored payload for "4242", and the
    payload carries the world's random ids. A tag that happened to contain those digits failed the test
    in CI although nothing leaked."""
    from tests.integration.test_webhooks_real_postgres import World

    draws = iter([
        uuid.UUID("4242abcdef0123456789abcdef012345"),  # the tag's draw: would be "4242abcdef"
        uuid.UUID("ab4242cdef0123456789abcdef012345"),  # the event id's first draw: would be "ab4242cd"
        uuid.UUID("a1b2c3d4e5f60718293a4b5c6d7e8f90"),
        uuid.UUID("b1b2c3d4e5f60718293a4b5c6d7e8f90"),
    ])
    monkeypatch.setattr(ids.uuid, "uuid4", lambda: next(draws))

    world = World("postgresql://not-used")
    event_id = world.event_id()

    for value in (world.tenant, world.customer, world.product, world.payment, event_id):
        assert "4242" not in value, value
