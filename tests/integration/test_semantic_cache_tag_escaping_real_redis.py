"""The answer cache's tenant/principal filter against a REAL Redis Stack.

tests/retrieval/test_semantic_cache_tag_escaping.py pins the query STRING the
cache builds. What a string test cannot prove is how RediSearch reads it — that
`|` inside a tag block is an OR, and that an escaped `\\|` is a literal that
still matches the value stored under it. Both are third-party behavior
(Constitution Principle VII: verify against the real thing), and both matter:
the first is why the old escape set let principal `alice|bob` read `bob`'s
cached answers; the second is why the fix must not make a legitimate principal
containing punctuation stop hitting its own entries.

Self-provisioned via `tests/containers.py::ensure_redis()` (skips cleanly
without Docker). Entries are scoped by a per-test unique tenant rather than
`flushall()` — the container is shared across tests and xdist workers.
"""
import hashlib
import uuid

import pytest

from app.retrieval import semantic_cache
from tests.containers import ensure_redis

pytestmark = pytest.mark.integration

QUESTION = "What is the refund policy?"


async def _fake_embed_text(text: str) -> list[float]:
    """Deterministic and network-free: same text, same vector. Isolation here is
    enforced by the TAG filter regardless of the vector's values, so a real
    embedding model is not load-bearing (same reasoning as
    tests/agent/test_concurrent_turns.py's `_fake_embed_text`)."""
    return [b / 255.0 for b in hashlib.sha256(text.encode()).digest()]


@pytest.fixture
def tenant(monkeypatch):
    info = ensure_redis()
    monkeypatch.setattr(semantic_cache, "REDIS_URL", info["redis_url"])
    monkeypatch.setattr(semantic_cache, "_client", None)
    monkeypatch.setattr(semantic_cache, "_index_ready", False)
    monkeypatch.setattr(semantic_cache, "embed_text", _fake_embed_text)
    return f"acme-{uuid.uuid4().hex[:8]}"


def _ctx(tenant, principal):
    return {"tenant": tenant, "principal": principal, "claims": {}}


async def test_a_principal_containing_the_or_operator_cannot_read_anothers_cached_answer(tenant):
    await semantic_cache.set(_ctx(tenant, "bob"), QUESTION, "bob's private answer", [])

    assert await semantic_cache.get(_ctx(tenant, "bob"), QUESTION) is not None, (
        "control: bob must hit his own entry, or the assertion below passes vacuously"
    )
    assert await semantic_cache.get(_ctx(tenant, "alice|bob"), QUESTION) is None


async def test_a_tenant_containing_the_or_operator_cannot_read_another_tenants_cached_answer(tenant):
    await semantic_cache.set(_ctx(tenant, "p"), QUESTION, "the other tenant's answer", [])

    assert await semantic_cache.get(_ctx(tenant, "p"), QUESTION) is not None, "control"
    assert await semantic_cache.get(_ctx(f"nobody-{uuid.uuid4().hex[:8]}|{tenant}", "p"), QUESTION) is None


@pytest.mark.parametrize("principal", ["o'neil", "a|b", "back\\slash", "a b", "x/y?z", "plus+minus-dot."])
async def test_a_principal_with_punctuation_still_hits_its_own_entry(tenant, principal):
    """The escape must round-trip: escaping is only correct if the value stored
    raw still matches the query built from it."""
    await semantic_cache.set(_ctx(tenant, principal), QUESTION, "mine", [])

    hit = await semantic_cache.get(_ctx(tenant, principal), QUESTION)

    assert hit is not None and hit[0] == "mine"
