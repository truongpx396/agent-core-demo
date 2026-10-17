"""The embedding-identity guard, the alias swap and the re-embed against a REAL Qdrant and a REAL Redis (specs/014 T057).

The hermetic tests (tests/retrieval/test_qdrant_identity.py, tests/scripts/test_reembed.py) use an in-memory fake that models Qdrant's alias
rules. A fake can drift from the server it imitates, so the claims the design rests on are proved here, once, against the real thing:

  * an alias swap (delete + create in ONE request) never lets a concurrent search fail;
  * a write sent to the alias lands in the physical collection behind it;
  * `scroll(with_vectors=True)` returns the NAMED sparse vector, which is why a re-embed can copy it instead of recomputing it;
  * the whole flow: a collection from another model is refused, `reembed` rebuilds it, retrieval works again, the old collection is kept,
    and a write that lands mid-run aborts the swap instead of being lost;
  * the answer cache's Redis index follows the identity, so old answers are never read under a new embedding.

The Qdrant server version it ran against is recorded as a test property (see the CI log / `-rP`).
Self-skips (never fails) without Docker, like every `integration` test.
"""
import asyncio
import uuid

import httpx
import pytest
from qdrant_client.models import PointStruct, SparseVector

from app.retrieval import embedding_identity, embeddings, qdrant_store, semantic_cache
from app.retrieval.embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityMismatch,
)
from scripts import reembed
from scripts.reembed import ReembedError
from tests.conftest import TEST_EMBEDDING_IDENTITY
from tests.containers import ensure_qdrant, ensure_redis

pytestmark = pytest.mark.integration

OTHER = EmbeddingIdentity("embed", "test/another-model", 3, "", "", "")
THIRD = EmbeddingIdentity("embed", "test/a-third-model", 3, "", "", "")


@pytest.fixture
def qdrant(monkeypatch, record_property):
    info = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", info["qdrant_url"])
    try:
        record_property("qdrant_server_version", httpx.get(info["qdrant_url"], timeout=5).json().get("version", "unknown"))
    except Exception:  # noqa: BLE001 - recording the version is a convenience; never a reason to fail the test
        record_property("qdrant_server_version", "unknown")
    return qdrant_store.get_client()


@pytest.fixture
def logical():
    return f"it-{uuid.uuid4().hex[:8]}"


def _vec(text: str) -> list[float]:
    return [float(len(text)), float(sum(map(ord, text)) % 97) + 1.0, 1.0]


async def _embed(texts):
    return [_vec(t) for t in texts]


def _point(i: int, text: str) -> PointStruct:
    return qdrant_store.build_point(
        point_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{text}-{i}")),
        dense_vector=_vec(text),
        sparse_vector=([i, i + 100], [0.5, 0.25]),
        payload={"text": text, "tenant": f"tenant-{i % 2}", "owner": f"user-{i}", "kind": "document"},
    )


def _use_identity(monkeypatch, identity):
    async def fixed():
        return identity

    monkeypatch.setattr(qdrant_store, "current_identity", fixed)
    monkeypatch.setattr(embedding_identity, "current_identity", fixed)
    qdrant_store.reset_identity_cache()


async def _stub_query_legs(monkeypatch):
    async def embed_query(text):
        return _vec(text)

    monkeypatch.setattr(embeddings, "embed_query", embed_query)
    monkeypatch.setattr(embeddings, "embed_sparse", lambda _t: ([1, 100], [0.5, 0.25]))


# ---- the three research claims -----------------------------------------------------------------------------------------------
async def test_swapping_the_alias_never_fails_a_search_running_at_the_same_time(qdrant, logical):
    a, b = f"{logical}-aaaaaaaa", f"{logical}-bbbbbbbb"
    for name in (a, b):
        await qdrant_store.create_physical(name, 3)
        await qdrant.upsert(collection_name=name, points=[_point(i, f"passage {i}") for i in range(5)])
    await qdrant_store.repoint_live(logical, a)
    failures: list[Exception] = []
    stop = asyncio.Event()

    async def search_loop():
        while not stop.is_set():
            try:
                await qdrant.query_points(collection_name=qdrant_store.live_name(logical), query=[1.0, 1.0, 1.0], using="dense", limit=1)
            except Exception as exc:  # noqa: BLE001 - the thing under test is "no search ever raises"
                failures.append(exc)
            await asyncio.sleep(0)

    searcher = asyncio.create_task(search_loop())
    for i in range(40):
        await qdrant_store.repoint_live(logical, b if i % 2 == 0 else a)
    stop.set()
    await searcher

    assert failures == []  # the delete+create pair is one atomic request: there is no instant at which the alias is missing
    assert await qdrant_store.alias_target(logical) in (a, b)


async def test_a_write_through_the_alias_lands_in_the_collection_behind_it(qdrant, logical, monkeypatch):
    await qdrant_store.ensure_collection(dim=3, collection=logical)
    physical = await qdrant_store.alias_target(logical)

    await qdrant_store.upsert([_point(1, "written through the alias")], collection=logical)

    count = await qdrant.count(collection_name=physical, exact=True)
    assert count.count == 1 and physical == qdrant_store.physical_name(logical, TEST_EMBEDDING_IDENTITY.hash8())


async def test_scroll_with_vectors_returns_the_named_sparse_vector(qdrant, logical):
    await qdrant_store.ensure_collection(dim=3, collection=logical)
    await qdrant_store.upsert([_point(7, "has a sparse vector")], collection=logical)
    physical = await qdrant_store.alias_target(logical)

    records, _next = await qdrant.scroll(collection_name=physical, limit=10, with_payload=True, with_vectors=True)

    sparse = records[0].vector["sparse"]
    assert isinstance(sparse, SparseVector) and list(sparse.indices) == [7, 107] and list(sparse.values) == [0.5, 0.25]


# ---- the whole flow ---------------------------------------------------------------------------------------------------------
async def test_a_collection_from_another_model_is_refused_rebuilt_and_served_again_with_the_old_one_kept(qdrant, logical, monkeypatch):
    await _stub_query_legs(monkeypatch)
    await qdrant_store.ensure_collection(dim=3, collection=logical)  # built under the configured identity
    await qdrant_store.upsert([_point(i, f"refund policy passage {i}") for i in range(6)], collection=logical)
    old_physical = await qdrant_store.alias_target(logical)
    assert len(await qdrant_store.hybrid_search("refund", collection=logical, rerank_results=False)) > 0  # works before

    _use_identity(monkeypatch, OTHER)  # the operator swaps the embedding model
    with pytest.raises(EmbeddingIdentityMismatch):
        await qdrant_store.hybrid_search("refund", collection=logical, rerank_results=False)
    with pytest.raises(EmbeddingIdentityMismatch):
        await qdrant_store.upsert([_point(99, "must not land")], collection=logical)

    report = await reembed.reembed(logical, embed=_embed)

    assert report.swapped and report.copied == 6
    hits = await qdrant_store.hybrid_search("refund", collection=logical, rerank_results=False)
    assert len(hits) > 0  # retrieval works under the new identity
    assert await qdrant_store.alias_target(logical) == qdrant_store.physical_name(logical, OTHER.hash8())
    assert await qdrant.collection_exists(old_physical)  # never deleted by the script
    new_records, _ = await qdrant.scroll(
        collection_name=await qdrant_store.alias_target(logical), limit=50, with_payload=True, with_vectors=True
    )
    old_records, _ = await qdrant.scroll(collection_name=old_physical, limit=50, with_payload=True, with_vectors=True)
    assert {str(r.id): r.payload for r in new_records} == {str(r.id): r.payload for r in old_records}  # payload byte-for-byte
    assert {str(r.id): r.vector["sparse"] for r in new_records} == {str(r.id): r.vector["sparse"] for r in old_records}  # sparse kept


async def test_a_write_landing_mid_run_aborts_the_swap_on_a_real_server(qdrant, logical, monkeypatch):
    await qdrant_store.ensure_collection(dim=3, collection=logical)
    await qdrant_store.upsert([_point(i, f"passage {i}") for i in range(4)], collection=logical)
    old_physical = await qdrant_store.alias_target(logical)
    _use_identity(monkeypatch, THIRD)

    async def embed_then_a_write_lands(texts):
        await qdrant.upsert(collection_name=old_physical, points=[_point(50, "arrived while the copy was running")])
        return [_vec(t) for t in texts]

    with pytest.raises(ReembedError) as err:
        await reembed.reembed(logical, embed=embed_then_a_write_lands)

    assert err.value.exit_code == reembed.EXIT_CHANGED
    assert await qdrant_store.alias_target(logical) == old_physical  # the old collection, now holding the new write, is still live


async def test_a_collection_from_before_identities_existed_is_adopted_then_replaced(qdrant, logical, monkeypatch):
    await _stub_query_legs(monkeypatch)
    await qdrant_store.create_physical(logical, 3)  # a plain physical collection named exactly `logical`, no alias: the legacy shape
    await qdrant.upsert(collection_name=logical, points=[_point(i, f"legacy passage {i}") for i in range(3)])

    assert len(await qdrant_store.hybrid_search("legacy", collection=logical, rerank_results=False)) > 0  # adopted: served, not refused
    assert await qdrant_store.alias_target(logical) == logical

    report = await reembed.reembed(logical, embed=_embed)

    assert report.swapped and await qdrant_store.alias_target(logical) == qdrant_store.physical_name(logical, TEST_EMBEDDING_IDENTITY.hash8())
    assert await qdrant.collection_exists(logical)  # the legacy collection is left for the operator to delete


# ---- the answer cache -------------------------------------------------------------------------------------------------------
async def test_the_answer_cache_never_serves_an_answer_made_under_another_embedding(monkeypatch):
    redis_info = ensure_redis()
    monkeypatch.setattr(semantic_cache, "REDIS_URL", redis_info["redis_url"])
    monkeypatch.setattr(semantic_cache, "_client", None)
    monkeypatch.setattr(semantic_cache, "_ready_indexes", set())
    dims = {"n": 3}

    async def embed_query(_text):
        return [1.0] * dims["n"]  # the same question embeds to the same vector, in whatever dimension is current

    monkeypatch.setattr(semantic_cache, "embed_query", embed_query)
    ctx = {"tenant": f"acme-{uuid.uuid4().hex[:6]}", "principal": "alice", "claims": {}}

    _use_identity(monkeypatch, TEST_EMBEDDING_IDENTITY)
    await semantic_cache.set(ctx, "what is the refund policy?", "five business days", [])
    assert await semantic_cache.get(ctx, "what is the refund policy?") is not None  # control: it hits under its own identity

    dims["n"] = 5  # a different model with a different dimension would make the OLD index unusable if it were shared
    _use_identity(monkeypatch, EmbeddingIdentity("embed", "test/five-dim-model", 5, "", "", ""))
    assert await semantic_cache.get(ctx, "what is the refund policy?") is None  # a clean miss, not an error and not the old answer
    await semantic_cache.set(ctx, "what is the refund policy?", "a new answer", [])
    answer, _ = await semantic_cache.get(ctx, "what is the refund policy?")
    assert answer == "a new answer"
