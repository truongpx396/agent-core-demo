"""The answer cache is namespaced by the embedding identity (specs/014 FR-018, app/retrieval/semantic_cache.py).

A cached entry holds an embedding. After a model, dimension or prefix change, comparing a new question with an old model's vectors either
hits on nonsense or, if the size changed, breaks the index. So each identity gets its own index and key prefix and old entries are simply
never read; they expire through the TTL. These tests use the real `_ensure_index` against a recording client; the real-Redis behavior is
in tests/integration/test_semantic_cache_tag_escaping_real_redis.py.
"""
from types import SimpleNamespace

import pytest

from app.retrieval import embedding_identity, semantic_cache
from app.retrieval.embedding_identity import EmbeddingIdentity
from tests.conftest import TEST_EMBEDDING_IDENTITY


class _Index:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    async def create_index(self, schema, definition):
        flat = [str(a) for field in schema for a in field.redis_args()]
        self.owner.created.append({"index": self.name, "args": flat, "definition": [str(a) for a in definition.args]})

    async def search(self, query, query_params=None):
        self.owner.searched.append(self.name)
        return SimpleNamespace(docs=[])


class _JSON:
    def __init__(self, owner):
        self.owner = owner

    async def set(self, key, path, value):
        self.owner.keys.append(key)


class _Client:
    def __init__(self):
        self.created, self.searched, self.keys, self.expired = [], [], [], []

    def ft(self, name):
        return _Index(self, name)

    def json(self):
        return _JSON(self)

    async def expire(self, key, seconds):
        self.expired.append(key)


@pytest.fixture
def cache(monkeypatch):
    client = _Client()
    monkeypatch.setattr(semantic_cache, "_get_client", lambda: client)
    monkeypatch.setattr(semantic_cache, "_ready_indexes", set())

    async def embed(_text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(semantic_cache, "embed_query", embed)
    return client


def _use(monkeypatch, identity):
    async def fixed():
        return identity

    monkeypatch.setattr(embedding_identity, "current_identity", fixed)


CTX = {"tenant": "acme", "principal": "alice", "claims": {}}


async def test_the_index_and_key_prefix_carry_the_identity_and_the_dimension_comes_from_it(cache):
    await semantic_cache.set(CTX, "q?", "a", [])

    h = TEST_EMBEDDING_IDENTITY.hash8()
    (created,) = cache.created
    assert created["index"] == f"idx:semantic_cache:{h}"
    assert f"cache:{h}:" in created["definition"]
    assert "DIM" in created["args"] and created["args"][created["args"].index("DIM") + 1] == "3"  # TEST identity's dimension
    assert cache.keys[0].startswith(f"cache:{h}:")


async def test_the_index_is_created_once_per_identity(cache):
    await semantic_cache.set(CTX, "q1", "a", [])
    await semantic_cache.set(CTX, "q2", "a", [])
    await semantic_cache.get(CTX, "q1")

    assert len(cache.created) == 1


async def test_a_new_identity_gets_a_new_index_and_prefix_and_never_reads_the_old_one(cache, monkeypatch):
    await semantic_cache.get(CTX, "q")
    new = EmbeddingIdentity("embed", "test/another-model", 5, "", "", "")
    _use(monkeypatch, new)
    await semantic_cache.set(CTX, "q", "a", [])
    await semantic_cache.get(CTX, "q")

    old_index, new_index = f"idx:semantic_cache:{TEST_EMBEDDING_IDENTITY.hash8()}", f"idx:semantic_cache:{new.hash8()}"
    assert [c["index"] for c in cache.created] == [old_index, new_index]
    assert cache.created[1]["args"][cache.created[1]["args"].index("DIM") + 1] == "5"  # a different dimension is a different index, not a broken one
    assert cache.searched == [old_index, new_index]  # the lookup under the new identity searched only the new index
    assert cache.keys[0].startswith(f"cache:{new.hash8()}:")


async def test_a_prefix_change_alone_is_a_new_namespace(cache, monkeypatch):
    plain = TEST_EMBEDDING_IDENTITY
    prefixed = EmbeddingIdentity(**{**plain.__dict__, "query_prefix": "search_query: "})
    _use(monkeypatch, prefixed)
    await semantic_cache.set(CTX, "q", "a", [])

    assert cache.created[0]["index"].endswith(prefixed.hash8()) and prefixed.hash8() != plain.hash8()
