"""`semantic_cache._escape_tag` must neutralize EVERY character RediSearch gives
meaning inside a TAG filter, because tenant and principal are interpolated into
the query string (`@tenant:{…} @principal:{…}`) — they are the cache's whole
isolation boundary (Principle I).

Real defect (fixed): the escape set covered `,.<>{}[]"':;!@#$%^&*()-+=~` and space
but not `|` — the OR operator inside a tag block — nor the backslash that
escapes, so a principal such as `alice|bob` built `@principal:{alice|bob}`, a
filter that ALSO matches `bob`'s cached answers (a cached answer can carry
citations to that principal's own memories). The earlier fix for "tenant
`other-co` raised a syntax error" (pattern 22) was a partial list added as each
character bit; this tests the property, not the list.

It needs a deployment where a principal or tenant string is user-influenced —
today's trusted-header seam already lets a caller *claim* any identity — but it
is the cache's isolation, so it is held to the same standard as the filter
itself.
"""
import string

import pytest

from app.retrieval import semantic_cache

# Everything an ASCII-punctuation or whitespace character could be, minus the one
# the TAG grammar treats as part of a word.
_SYNTAX = [c for c in string.punctuation + " \t\n" if c != "_"]


@pytest.mark.parametrize("char", _SYNTAX, ids=[repr(c) for c in _SYNTAX])
def test_every_ascii_punctuation_or_whitespace_character_is_escaped(char):
    assert semantic_cache._escape_tag(f"a{char}b") == f"a\\{char}b"


@pytest.mark.parametrize("value", ["ecorp", "ecorp_2", "Alice01", "tenant-é", "日本語"], ids=repr)
def test_word_characters_and_non_ascii_letters_pass_through(value):
    expected = value.replace("-", r"\-")
    assert semantic_cache._escape_tag(value) == expected


def test_a_backslash_cannot_be_used_to_unescape_the_next_character():
    r"""`a\|b`: if the backslash were left alone the escaped pipe would read as
    `\\` (a literal backslash) followed by a live `|`."""
    assert semantic_cache._escape_tag("a\\|b") == "a\\\\\\|b"


class _RecordingClient:
    def __init__(self):
        self.queries = []

    def ft(self, _index):
        return self

    async def search(self, query, query_params=None):
        self.queries.append(query.query_string())
        return type("Result", (), {"docs": []})()


@pytest.fixture
def client(monkeypatch):
    recording = _RecordingClient()

    async def no_index(_client):
        return None

    async def fake_embed(_text):
        return [0.0, 0.0]

    monkeypatch.setattr(semantic_cache, "_get_client", lambda: recording)
    monkeypatch.setattr(semantic_cache, "_ensure_index", no_index)
    monkeypatch.setattr(semantic_cache, "embed_text", fake_embed)
    return recording


async def test_a_principal_containing_the_or_operator_cannot_widen_the_filter(client):
    ctx = {"tenant": "acme", "principal": "alice|bob", "claims": {}}

    await semantic_cache.get(ctx, "hello")

    assert client.queries == [r"(@tenant:{acme} @principal:{alice\|bob})=>[KNN 1 @embedding $vec AS dist]"]


def _unescaped(query: str) -> list[str]:
    """The characters of `query` that are NOT escaped, scanned the way RediSearch
    reads it — a backslash consumes the next character, so `\\}` is an escaped
    backslash followed by a LIVE brace (a lookbehind regex gets that wrong)."""
    live, i = [], 0
    while i < len(query):
        if query[i] == "\\":
            i += 2
            continue
        live.append(query[i])
        i += 1
    return live


async def test_a_value_shaped_like_query_syntax_leaves_exactly_the_two_tag_blocks(client):
    """Exactly the two tag blocks of the query must survive — four live braces —
    and no live `|` or parenthesis other than the filter's own pair."""
    ctx = {"tenant": "a}) | (@tenant:{b", "principal": "p\\", "claims": {}}

    await semantic_cache.get(ctx, "hello")

    (query,) = client.queries
    live = _unescaped(query.split("=>")[0])
    assert live.count("{") == 2 and live.count("}") == 2, query
    assert "|" not in live, query
    assert live.count("(") == 1 and live.count(")") == 1, query
