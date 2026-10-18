"""The embedding-identity guard in `qdrant_store`: every state a collection can be in, and what each one costs a reader and a writer.

The failure this guards against is silent: two embedding models of the same dimension are interchangeable to Qdrant, so a swap that
forgets to rebuild the collection makes every search return confident nonsense with no error anywhere. The guard turns that into a
refusal that names the fix. Against an in-memory Qdrant (tests/retrieval/fake_qdrant.py) that enforces the alias rules this relies on;
the same claims are proved against a real Qdrant in tests/integration/test_embedding_identity_real_qdrant.py.
"""
from types import SimpleNamespace

import pytest
from qdrant_client.models import Filter, PointStruct

from app.retrieval import embeddings, qdrant_store
from app.retrieval.embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityMismatch,
)
from tests.conftest import TEST_EMBEDDING_IDENTITY
from tests.retrieval.fake_qdrant import FakeQdrant, NotFound

H = TEST_EMBEDDING_IDENTITY.hash8()
OTHER = EmbeddingIdentity("embed", "test/another-model", 3, "", "", "")
OTHER_H = OTHER.hash8()


class RecordingCounter:
    """Stands in for the OpenTelemetry-backed counter: records (outcome, collection) for every increment."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def labels(self, **labels):
        return SimpleNamespace(inc=lambda amount=1: self.events.append((labels["outcome"], labels["collection"])))

    def outcomes(self, collection="docs"):
        return [o for o, c in self.events if c == collection]


@pytest.fixture
def world(monkeypatch):
    fake, counter = FakeQdrant(), RecordingCounter()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)
    monkeypatch.setattr(qdrant_store.metrics, "agent_embedding_identity_total", counter)

    async def embed_query(_text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(embeddings, "embed_query", embed_query)
    monkeypatch.setattr(embeddings, "embed_sparse", lambda _t: ([1], [0.5]))
    return SimpleNamespace(qdrant=fake, counter=counter)


def _live_collection(world, identity_hash, name="docs"):
    physical = qdrant_store.physical_name(name, identity_hash)
    world.qdrant.seed(physical)
    world.qdrant.aliases[qdrant_store.live_name(name)] = physical
    return physical


def _point(i=1):
    return PointStruct(id=str(i), vector={"dense": [0.1, 0.2, 0.3]}, payload={"text": "t", "tenant": "acme"})


def _use_identity(monkeypatch, identity):
    async def fixed():
        return identity

    monkeypatch.setattr(qdrant_store, "current_identity", fixed)


# ---- creating a collection ---------------------------------------------------------------------------------------------------
async def test_a_new_collection_is_named_after_the_identity_and_reached_through_the_live_alias(world):
    await qdrant_store.ensure_collection(dim=3)

    assert list(world.qdrant.collections) == [f"docs-{H}"]
    assert world.qdrant.aliases == {"docs-live": f"docs-{H}"}


async def test_recreating_under_the_same_identity_replaces_that_collection_and_swaps_the_alias_in_one_request(world):
    await qdrant_store.ensure_collection(dim=3)
    await qdrant_store.upsert([_point()])
    await qdrant_store.ensure_collection(dim=3)

    assert list(world.qdrant.collections) == [f"docs-{H}"]
    assert world.qdrant.collections[f"docs-{H}"]["points"] == {}  # `make ingest` re-seeds from scratch, by design
    # the second swap is ONE request carrying a delete and a create (atomic), not two requests with a gap between them
    assert [c for c in world.qdrant.calls if c[0] == "update_collection_aliases"] == [("update_collection_aliases", 1), ("update_collection_aliases", 2)]


async def test_creating_under_a_different_identity_leaves_the_old_collection_in_place_and_makes_the_new_one_live(world, monkeypatch):
    await qdrant_store.ensure_collection(dim=3)
    _use_identity(monkeypatch, OTHER)
    await qdrant_store.ensure_collection(dim=3)

    assert set(world.qdrant.collections) == {f"docs-{H}", f"docs-{OTHER_H}"}  # nothing was deleted
    assert world.qdrant.aliases["docs-live"] == f"docs-{OTHER_H}"


# ---- reading and writing a verified collection -----------------------------------------------------------------------------
async def test_a_collection_built_under_the_configured_identity_is_read_and_written_through_the_alias(world):
    _live_collection(world, H)

    await qdrant_store.upsert([_point()])
    await qdrant_store.hybrid_search("hello")

    assert ("upsert", "docs-live", 1) in world.qdrant.calls and ("query_points", "docs-live") in world.qdrant.calls
    assert world.counter.outcomes() == ["verified"]  # counted when the answer was refreshed, not once per request


async def test_the_check_is_cached_so_a_search_costs_no_extra_round_trip(world):
    _live_collection(world, H)

    for _ in range(5):
        await qdrant_store.hybrid_search("hello")

    assert [c for c in world.qdrant.calls if c[0] == "scroll"] == []
    assert world.counter.outcomes() == ["verified"]


async def test_an_identity_that_could_not_be_resolved_is_still_served_and_counted_as_unverified(world, monkeypatch):
    unresolved = EmbeddingIdentity("embed", None, 3, "", "", "")
    _use_identity(monkeypatch, unresolved)
    _live_collection(world, unresolved.hash8())

    await qdrant_store.upsert([_point()])

    assert world.counter.outcomes() == ["unverified"]  # a deployment with no LiteLLM admin endpoint keeps working; it is just weaker


# ---- a mismatch is refused ---------------------------------------------------------------------------------------------------
async def test_a_search_over_a_collection_from_another_model_is_refused_and_names_the_fix(world):
    _live_collection(world, OTHER_H)

    with pytest.raises(EmbeddingIdentityMismatch) as refused:
        await qdrant_store.hybrid_search("hello")

    assert refused.value.expected == H and refused.value.found == OTHER_H and refused.value.write is False
    assert "make reembed COLLECTION_NAME=docs" in str(refused.value)
    assert world.counter.outcomes() == ["mismatch"]


async def test_the_refusal_comes_before_the_query_embedding_is_spent(world, monkeypatch):
    _live_collection(world, OTHER_H)
    spent = []

    async def embed_query(_text):
        spent.append(_text)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(embeddings, "embed_query", embed_query)

    with pytest.raises(EmbeddingIdentityMismatch):
        await qdrant_store.hybrid_search("hello")

    assert spent == [] and not any(c[0] == "query_points" for c in world.qdrant.calls)


async def test_a_write_to_a_collection_from_another_model_is_refused_and_nothing_is_stored(world):
    physical = _live_collection(world, OTHER_H)

    with pytest.raises(EmbeddingIdentityMismatch) as refused:
        await qdrant_store.upsert([_point()])

    assert refused.value.write is True
    assert world.qdrant.collections[physical]["points"] == {}  # the vector never reached the collection


async def test_turning_a_prefix_on_makes_every_existing_collection_a_mismatch(world, monkeypatch):
    """The model is unchanged; only the prefix is. nomic's `search_query: ` moves queries to a different region than the unprefixed
    vectors already stored, so this must be refused exactly like a model swap."""
    _live_collection(world, H)
    await qdrant_store.hybrid_search("hello")  # fine before
    qdrant_store.reset_identity_cache()
    prefixed = EmbeddingIdentity(**{**TEST_EMBEDDING_IDENTITY.__dict__, "query_prefix": "search_query: "})
    _use_identity(monkeypatch, prefixed)

    with pytest.raises(EmbeddingIdentityMismatch):
        await qdrant_store.hybrid_search("hello")


async def test_every_refused_call_is_counted_so_the_alert_keeps_firing_while_requests_keep_failing(world):
    _live_collection(world, OTHER_H)

    for _ in range(3):
        with pytest.raises(EmbeddingIdentityMismatch):
            await qdrant_store.hybrid_search("hello")

    assert world.counter.outcomes().count("mismatch") == 3


async def test_a_data_subject_delete_and_a_count_still_work_on_a_mismatched_collection(world):
    """These touch no vector, and being able to delete a person's memories must not depend on the embedding settings."""
    _live_collection(world, OTHER_H)

    await qdrant_store.delete_by_filter(Filter())
    assert await qdrant_store.count_by_filter(Filter()) == 0

    assert ("delete", "docs-live") in world.qdrant.calls and world.counter.outcomes() == []


async def test_collections_are_judged_independently(world):
    _live_collection(world, H, "docs")
    _live_collection(world, OTHER_H, "skills")

    await qdrant_store.hybrid_search("hello")  # docs is fine
    with pytest.raises(EmbeddingIdentityMismatch):
        await qdrant_store.hybrid_search("hello", collection="skills")


# ---- a collection that predates all this ------------------------------------------------------------------------------------
async def test_a_legacy_collection_is_adopted_with_a_non_destructive_alias_and_counted(world):
    world.qdrant.seed("docs", [_point(1)])  # a physical collection named exactly `docs`, no alias, from before this existed

    await qdrant_store.upsert([_point(2)])

    assert world.qdrant.aliases == {"docs-live": "docs"}
    assert set(world.qdrant.collections["docs"]["points"]) == {"1", "2"}  # nothing was copied, moved or deleted
    assert world.counter.outcomes() == ["adopted"]


async def test_a_legacy_collection_is_trusted_whatever_the_configured_identity_is(world, monkeypatch):
    """Nothing records what it was made with, so adoption cannot be a check. This is the one weaker place in the guard, disclosed in the
    pattern; the first re-embed replaces the collection with a verifiable one."""
    world.qdrant.seed("docs")
    _use_identity(monkeypatch, OTHER)

    await qdrant_store.hybrid_search("hello")  # no refusal


async def test_losing_the_race_to_create_the_adoption_alias_is_not_an_error(world):
    world.qdrant.seed("docs")
    original = world.qdrant.update_collection_aliases

    async def lose_the_race(change_aliases_operations, **kw):
        world.qdrant.aliases["docs-live"] = "docs"  # another process created it a moment ago
        raise ValueError("`docs-live` already exists")

    world.qdrant.update_collection_aliases = lose_the_race
    try:
        assert await qdrant_store.ensure_live("docs") == "docs"
    finally:
        world.qdrant.update_collection_aliases = original


async def test_an_alias_failure_that_is_not_a_lost_race_is_raised(world):
    world.qdrant.seed("docs")
    world.qdrant.fail_next_alias_update = RuntimeError("qdrant is unwell")

    with pytest.raises(RuntimeError, match="unwell"):
        await qdrant_store.ensure_live("docs")


async def test_with_nothing_there_yet_a_fresh_install_reports_its_own_missing_collection_not_an_identity_error(world):
    with pytest.raises(NotFound):
        await qdrant_store.upsert([_point()])
    assert world.counter.events == []


# ---- the cache window ----------------------------------------------------------------------------------------------------
async def test_another_process_swapping_the_alias_is_noticed_within_the_cache_window(world, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(qdrant_store, "time", SimpleNamespace(monotonic=lambda: now["t"]))
    monkeypatch.setattr(qdrant_store, "EMBED_IDENTITY_CACHE_SECONDS", 10.0)
    _live_collection(world, H)
    await qdrant_store.hybrid_search("hello")  # cached: alias -> docs-H

    _live_collection(world, OTHER_H)  # an operator's re-embed elsewhere repoints docs-live at a collection of another identity
    world.qdrant.aliases["docs-live"] = f"docs-{OTHER_H}"
    now["t"] += 5.0
    await qdrant_store.hybrid_search("hello")  # inside the window: still the cached answer, still served

    now["t"] += 6.0
    with pytest.raises(EmbeddingIdentityMismatch):  # past the window: the new target is read and judged
        await qdrant_store.hybrid_search("hello")


async def test_the_process_that_repoints_the_alias_sees_it_at_once(world):
    await qdrant_store.ensure_collection(dim=3)
    await qdrant_store.hybrid_search("hello")  # cached

    world.qdrant.seed(f"docs-{OTHER_H}")
    await qdrant_store.repoint_live("docs", f"docs-{OTHER_H}")

    with pytest.raises(EmbeddingIdentityMismatch):  # no waiting for the TTL: repointing clears this process's cache
        await qdrant_store.hybrid_search("hello")
