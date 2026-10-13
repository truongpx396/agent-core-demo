"""app/billing/display.py: provider- and token-supplied text is shown, never obeyed, on an operator's terminal."""
import pytest

from app.billing.display import printable


@pytest.mark.parametrize("text", ["acme", "pilot top-up, ticket 4412", "naïve café 日本", "", "a b"])
def test_ordinary_text_is_shown_as_it_is(text):
    assert printable(text) == text


@pytest.mark.parametrize(
    ("hostile", "shown"),
    [
        ("\x1b[2J\x1b[Hall clear", "\\x1b[2J\\x1b[Hall clear"),  # clear the screen and home the cursor
        ("acme\rforged: balance 9999", "acme\\rforged: balance 9999"),  # a carriage return rewrites the line
        ("line one\nline two", "line one\\nline two"),
        ("bell\x07", "bell\\x07"),
        ("nul\x00byte", "nul\\x00byte"),
    ],
)
def test_control_characters_are_shown_as_their_escape_and_stay_inert(hostile, shown):
    assert printable(hostile) == shown
    assert printable(hostile).isprintable()


def test_none_is_nothing_and_other_types_are_their_text():
    assert printable(None) == "" and printable(42) == "42"
