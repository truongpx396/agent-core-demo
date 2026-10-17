"""scripts/reembed.py: rebuild a collection under the current embedding identity with no mixed moment (specs/014 FR-020).

Against the shared in-memory Qdrant fake. What matters, in order: the live collection is never touched until the last step; a payload
(tenant and owner included) arrives byte-for-byte; the sparse vector is kept, not recomputed; a write that lands mid-run stops the swap
instead of being lost; and every refusal says which exit code a script wrapping this should expect.
"""
import ast
from pathlib import Path

import pytest
from qdrant_client.models import PointStruct, SparseVector

from app.retrieval import qdrant_store
from scripts import reembed
from scripts.reembed import ReembedError
from tests.conftest import TEST_EMBEDDING_IDENTITY
from tests.retrieval.fake_qdrant import FakeQdrant

H = TEST_EMBEDDING_IDENTITY.hash8()
OLD = "ffffffff"  # an identity this process is not configured for
REPO = Path(__file__).resolve().parents[2]


def _text_vector(text: str) -> list[float]:
    return [float(len(text)), float(sum(map(ord, text)) % 97) + 1.0, 1.0]


def _point(i: int, *, text: str | None = None, sparse: bool = True, **payload) -> PointStruct:
    body = {"tenant": f"tenant-{i % 2}", "owner": f"user-{i}", "kind": "document", "title": f"Doc {i}", **payload}
    if text is not None:
        body["text"] = text
    vector = {"dense": [9.0, 9.0, 9.0]}  # an OLD-space vector: it must be replaced, not copied
    if sparse:
        vector["sparse"] = SparseVector(indices=[i, i + 1], values=[0.5, 0.25])
    return PointStruct(id=str(i), vector=vector, payload=body)


class Embedder:
    """Records exactly which texts were embedded, in order, and can be told to fail or to misbehave mid-run."""

    def __init__(self):
        self.texts: list[str] = []
        self.calls = 0
        self.fail_with: Exception | None = None
        self.before_return = None

    async def __call__(self, texts):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.texts.extend(texts)
        if self.before_return is not None:
            self.before_return()
        return [_text_vector(t) for t in texts]


@pytest.fixture
def world(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)
    return fake


def _live_old(world, n=5, **kw):
    old = f"docs-{OLD}"
    world.seed(old, [_point(i, text=f"passage number {i} about refunds", **kw) for i in range(n)])
    world.aliases["docs-live"] = old
    return old


# ---- the happy path ----------------------------------------------------------------------------------------------------------
async def test_every_point_is_copied_with_its_id_payload_and_sparse_vector_and_a_freshly_embedded_dense_one(world):
    old = _live_old(world)
    embed = Embedder()

    report = await reembed.reembed("docs", embed=embed)

    target = world.collections[f"docs-{H}"]["points"]
    source = world.collections[old]["points"]
    assert report.swapped and report.copied == 5 and set(target) == set(source)
    for pid, point in target.items():
        assert point.payload == source[pid].payload  # byte-for-byte: the tenant and owner scoping fields included (Principle I)
        assert point.vector["sparse"] == source[pid].vector["sparse"]  # kept, not recomputed
        assert point.vector["dense"] == _text_vector(source[pid].payload["text"])  # re-embedded from the stored text, not copied
        assert point.vector["dense"] != source[pid].vector["dense"]
    assert sorted(embed.texts) == sorted(p.payload["text"] for p in source.values())  # the exact text that was embedded originally


async def test_the_swap_is_one_atomic_alias_request_and_the_old_collection_is_left_in_place(world):
    old = _live_old(world)

    await reembed.reembed("docs", embed=Embedder())

    assert world.aliases == {"docs-live": f"docs-{H}"}
    assert old in world.collections  # never deleted by this script; an operator removes it after a bake period
    swaps = [c for c in world.calls if c[0] == "update_collection_aliases"]
    assert swaps[-1] == ("update_collection_aliases", 2)  # a delete and a create in ONE request, not two with a gap between


async def test_the_live_collection_is_untouched_until_the_final_step(world):
    old = _live_old(world)
    seen = {}

    def spy():  # runs inside the embedding call, i.e. mid-copy
        seen["alias"] = dict(world.aliases)
        seen["old_points"] = len(world.collections[old]["points"])

    embed = Embedder()
    embed.before_return = spy
    await reembed.reembed("docs", embed=embed)

    assert seen == {"alias": {"docs-live": old}, "old_points": 5}  # still serving the old identity while the new one was being built


async def test_a_point_without_a_sparse_vector_stays_without_one(world):
    old = f"docs-{OLD}"
    world.seed(old, [_point(1, text="dense only", sparse=False)])
    world.aliases["docs-live"] = old

    await reembed.reembed("docs", embed=Embedder())

    assert "sparse" not in world.collections[f"docs-{H}"]["points"]["1"].vector


async def test_running_it_again_is_a_no_op_because_the_collection_is_already_current(world):
    _live_old(world)
    await reembed.reembed("docs", embed=Embedder())
    embed = Embedder()

    report = await reembed.reembed("docs", embed=embed)

    assert report.already_current and embed.calls == 0
    assert "already under the configured embedding identity" in report.lines()[0]


async def test_a_dry_run_reports_the_plan_and_writes_nothing(world):
    _live_old(world)
    embed = Embedder()

    report = await reembed.reembed("docs", dry_run=True, embed=embed)

    assert report.points == 5 and not report.swapped and embed.calls == 0
    assert f"docs-{H}" not in world.collections and world.aliases == {"docs-live": f"docs-{OLD}"}


# ---- a legacy collection ---------------------------------------------------------------------------------------------------
async def test_a_legacy_collection_is_adopted_then_replaced_by_a_verifiable_one(world):
    world.seed("docs", [_point(1, text="legacy passage")])  # a physical `docs` with no alias, from before identities existed

    report = await reembed.reembed("docs", embed=Embedder())

    assert report.swapped and report.source == "docs"
    assert world.aliases["docs-live"] == f"docs-{H}"
    assert "docs" in world.collections  # the legacy collection survives for the operator to delete


async def test_with_no_collection_at_all_there_is_nothing_to_re_embed(world):
    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=Embedder())
    assert err.value.exit_code == reembed.EXIT_ERROR and "make ingest" in str(err.value)


# ---- points that cannot be re-embedded -----------------------------------------------------------------------------------
async def test_a_point_with_no_payload_text_blocks_the_swap_and_names_the_way_out(world):
    old = _live_old(world, n=3)
    world.collections[old]["points"]["9"] = _point(9)  # no payload["text"]

    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=Embedder())

    assert err.value.exit_code == reembed.EXIT_CHANGED and "--allow-skips" in str(err.value)
    assert world.aliases == {"docs-live": old}  # not swapped


async def test_listing_the_ids_in_allow_skips_leaves_them_out_and_swaps(world):
    old = _live_old(world, n=3)
    world.collections[old]["points"]["9"] = _point(9)

    report = await reembed.reembed("docs", embed=Embedder(), allow_skips=frozenset({"9"}))

    assert report.swapped and report.skipped == ["9"]
    assert "9" not in world.collections[f"docs-{H}"]["points"]


# ---- a write lands mid-run ---------------------------------------------------------------------------------------------------
async def test_a_write_to_the_source_during_the_run_stops_the_swap_instead_of_being_lost(world):
    old = _live_old(world)
    embed = Embedder()

    def a_write_lands():
        world.collections[old]["points"]["99"] = _point(99, text="added while the copy was running")

    embed.before_return = a_write_lands

    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=embed)

    assert err.value.exit_code == reembed.EXIT_CHANGED and "changed while it was being copied" in str(err.value)
    assert world.aliases == {"docs-live": old}  # the old collection, with the new write in it, is still the live one


# ---- failures leave the old collection serving -----------------------------------------------------------------------------
async def test_an_embedding_outage_is_exit_3_and_nothing_is_swapped(world):
    old = _live_old(world)
    embed = Embedder()
    embed.fail_with = ConnectionError("litellm unreachable")

    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=embed)

    assert err.value.exit_code == reembed.EXIT_EMBED_DOWN and "ConnectionError" in str(err.value)
    assert world.aliases == {"docs-live": old}


async def test_a_new_collection_missing_points_after_the_copy_is_not_swapped(world):
    old = _live_old(world)
    original = world.upsert

    async def lossy(collection_name, points):  # the target silently loses the last point
        await original(collection_name, points[:-1])

    world.upsert = lossy

    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=Embedder())

    assert err.value.exit_code == reembed.EXIT_CHANGED and "expected 5" in str(err.value)
    assert world.aliases == {"docs-live": old}


async def test_a_sample_point_that_does_not_find_itself_stops_the_swap(world):
    old = _live_old(world)
    original = world.query_points

    async def wrong_hit(collection_name, **kw):
        result = await original(collection_name, **kw)
        for hit in result.points:
            hit.id, hit.payload = "somebody-else", {"text": "a different passage"}
        return result

    world.query_points = wrong_hit

    with pytest.raises(ReembedError) as err:
        await reembed.reembed("docs", embed=Embedder())

    assert err.value.exit_code == reembed.EXIT_CHANGED and "does not find itself" in str(err.value)
    assert world.aliases == {"docs-live": old}


# ---- resume ------------------------------------------------------------------------------------------------------------------
async def test_resume_reuses_a_half_built_target_while_the_default_recreates_it(world):
    _live_old(world, n=4)
    world.seed(f"docs-{H}", [PointStruct(id="0", vector={"dense": [1.0, 1.0, 1.0]}, payload={"stale": True})])

    await reembed.reembed("docs", embed=Embedder(), resume=True)
    assert not any(c == ("recreate_collection", f"docs-{H}") for c in world.calls)  # reused, not wiped

    world.aliases["docs-live"] = f"docs-{OLD}"
    await reembed.reembed("docs", embed=Embedder())
    assert any(c == ("recreate_collection", f"docs-{H}") for c in world.calls)  # without --resume it starts clean


# ---- the command line --------------------------------------------------------------------------------------------------------
def test_an_unknown_collection_is_a_usage_error(world, capsys):
    assert reembed.main(["not-a-collection"]) == reembed.EXIT_USAGE
    assert "unknown collection" in capsys.readouterr().err


def test_a_page_size_below_one_is_a_usage_error(world):
    assert reembed.main(["docs", "--page", "0"]) == reembed.EXIT_USAGE


def test_a_dry_run_from_the_command_line_prints_the_plan_and_exits_zero(world, capsys):
    _live_old(world)
    assert reembed.main(["docs", "--dry-run"]) == reembed.EXIT_OK
    assert "5 points" in capsys.readouterr().out


def test_an_unexpected_failure_is_exit_1_naming_the_class_never_a_payload(world, monkeypatch, capsys):
    async def boom(*a, **k):
        raise RuntimeError("tenant-0 owner-3 secret payload text")

    monkeypatch.setattr(reembed, "reembed", boom)

    assert reembed.main(["docs"]) == reembed.EXIT_ERROR
    # the message is reported (it is the operator's terminal), but the script itself never logs or prints a payload it read
    assert "old collection is untouched" in capsys.readouterr().err


# ---- it is an operator tool, not part of the app ---------------------------------------------------------------------------
def test_nothing_under_app_can_import_the_reembed_script():
    """It reads every tenant's points by design (a collection is not per-tenant); that is acceptable for a script an operator runs and
    never for anything the agent loop or the API can reach."""
    offenders = []
    for path in sorted((REPO / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[(node.module or "") + "." + a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n == "scripts.reembed" or n.endswith(".reembed") and n.startswith("scripts") for n in names):
                offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == []
