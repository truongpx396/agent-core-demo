"""Hermetic tests for the answer cache's tenant/principal scoping in
app/retrieval/semantic_cache.py — the part that is easiest to get wrong and
costs most when wrong (a cached answer can carry citations to one principal's
own memories).

Until now the `_escape_tag` guarantee — the fix for "tenant `other-co` raised a
RediSearch syntax error" (GRAPH_PATTERNS.md pattern 22) — was exercised only
*incidentally*, by a real-Redis concurrency test that happens to use hyphenated
tenant names. These pin it by name, without Redis, and cover the principal axis,
which no test touched. (Escaping of the *full* syntax-character set is tested
with its own fix, in test_semantic_cache_tag_escaping.py.)
"""
import pytest

from app.retrieval import semantic_cache


class TestEscapeTag:
    def test_a_hyphenated_tenant_is_escaped(self):
        assert semantic_cache._escape_tag("other-co") == r"other\-co"

    def test_ordinary_identifiers_pass_through_unchanged(self):
        assert semantic_cache._escape_tag("ecorp_2") == "ecorp_2"


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


class TestGetQueryIsScopedAndEscaped:
    async def test_the_query_restricts_to_both_tenant_and_principal(self, client):
        ctx = {"tenant": "acme", "principal": "alice", "claims": {}}

        await semantic_cache.get(ctx, "what is the refund policy?")

        assert client.queries == ["(@tenant:{acme} @principal:{alice})=>[KNN 1 @embedding $vec AS dist]"]

    async def test_syntax_characters_in_either_axis_are_escaped(self, client):
        """`other-co` raised a syntax error before the fix; the principal axis
        was never covered. Both go through `_escape_tag`."""
        ctx = {"tenant": "other-co.eu", "principal": "a@b c", "claims": {}}

        await semantic_cache.get(ctx, "hello")

        assert client.queries == [
            r"(@tenant:{other\-co\.eu} @principal:{a\@b\ c})=>[KNN 1 @embedding $vec AS dist]"
        ]

    @pytest.mark.parametrize(
        "ctx",
        [None, {}, {"tenant": "acme", "principal": "", "claims": {}}, {"tenant": "", "principal": "p", "claims": {}}],
        ids=["no-ctx", "empty-ctx", "no-principal", "no-tenant"],
    )
    async def test_an_incomplete_ctx_is_a_miss_that_never_queries(self, client, ctx):
        assert await semantic_cache.get(ctx, "hello") is None
        assert client.queries == []
