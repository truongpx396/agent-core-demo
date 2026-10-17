"""Dataset verification and the public-dataset rules (scripts/model_eval/datasets.py)."""
import json
from pathlib import Path

import pytest

from scripts.model_eval import datasets as ds
from scripts.model_eval.datasets import DatasetError

SHA = "a" * 40
QUERIES = {
    "dev": ["how do I reset my password", "what is the refund window for annual plans", "where can I download my invoices"],
    "test": ["which regions does the support team cover overnight", "how are escalations to engineering approved", "can a sales rep edit a closed deal"],
}


def make_dataset(root: Path, *, queries=None, licence="repository-internal", provenance=None, reviewed=(False, False, False), kind="embedding") -> Path:
    queries = queries or QUERIES
    root.mkdir(parents=True, exist_ok=True)
    splits = {}
    for name, texts in queries.items():
        rows = [{"qid": f"{name}-{i}", "query": t, "relevant": {"c1": 1}, "reviewed": reviewed[i % len(reviewed)]} for i, t in enumerate(texts)]
        splits[name] = {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(root / f"{name}.jsonl", rows), "items": len(rows)}
    corpus = [{"id": "c1", "text": "some passage"}]
    manifest = {
        "schema_version": 1, "name": "retrieval-domains", "version": "v1", "kind": kind, "licence": licence,
        "provenance": provenance if provenance is not None else {"generator": "test-generator"},
        "splits": splits, "corpus": {"file": "corpus.jsonl", "sha256": ds.write_jsonl(root / "corpus.jsonl", corpus), "chunks": 1},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_a_well_formed_dataset_verifies_clean_and_its_reviewed_share_is_computed_from_the_items(tmp_path):
    root = make_dataset(tmp_path / "d", reviewed=(True, False, False))

    assert ds.verify_dataset(root) == []
    manifest = ds.load_verified(root)
    assert ds.dataset_summary(manifest)["reviewed_share"] == pytest.approx(1 / 3)  # test split: one reviewed item of three
    assert ds.dataset_summary(manifest)["items"] == 3


def test_a_one_byte_change_to_a_data_file_stops_the_run(tmp_path):
    root = make_dataset(tmp_path / "d")
    path = root / "test.jsonl"
    path.write_text(path.read_text().replace("overnight", "overnigh7"))

    problems = ds.verify_dataset(root)

    assert any("test.jsonl does not match its recorded sha256" in p for p in problems)
    with pytest.raises(DatasetError, match="verification failed"):
        ds.load_verified(root)


def test_dev_and_test_sharing_an_item_id_is_a_problem(tmp_path):
    root = make_dataset(tmp_path / "d")
    dev = ds.read_jsonl(root / "dev.jsonl")
    test = ds.read_jsonl(root / "test.jsonl")
    test[0]["qid"] = dev[0]["qid"]
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["splits"]["test"]["sha256"] = ds.write_jsonl(root / "test.jsonl", test)
    (root / "manifest.json").write_text(json.dumps(manifest))

    assert any("share 1 item ids" in p for p in ds.verify_dataset(root))


def test_a_near_duplicate_across_the_split_is_a_leak(tmp_path):
    leaky = {"dev": ["how do I reset my password today", "unrelated question about invoices"], "test": ["how do i reset my password today", "something about escalation paths"]}
    root = make_dataset(tmp_path / "d", queries=leaky, reviewed=(True,))

    assert any("near-duplicates" in p for p in ds.verify_dataset(root))


def test_an_item_that_mentions_a_tenant_identifier_is_refused(tmp_path):
    bad = {"dev": ["where is the acme invoice"], "test": ["how are escalations approved"]}
    assert any("looks like tenant data" in p for p in ds.verify_dataset(make_dataset(tmp_path / "d", queries=bad)))


@pytest.mark.parametrize(("licence", "ok"), [("repository-internal", True), ("MIT", True), ("CC-BY-SA-4.0", True), ("other", False), ("CC-BY-NC-4.0", False), ("", False)])
def test_only_allow_listed_licences_or_the_internal_marker_pass(tmp_path, licence, ok):
    problems = ds.verify_dataset(make_dataset(tmp_path / "d", licence=licence))
    assert (problems == []) is ok


def test_provenance_must_name_a_generator_or_an_author(tmp_path):
    assert any("provenance" in p for p in ds.verify_dataset(make_dataset(tmp_path / "d", provenance={"filters": {}})))
    assert ds.verify_dataset(make_dataset(tmp_path / "e", provenance={"author": "a person"})) == []


def test_a_row_count_that_disagrees_with_the_manifest_is_a_problem(tmp_path):
    root = make_dataset(tmp_path / "d")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["splits"]["test"]["items"] = 99
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert any("99" in p for p in ds.verify_dataset(root))


def test_a_directory_with_no_manifest_is_a_problem_not_a_crash(tmp_path):
    assert ds.verify_dataset(tmp_path) == [f"{tmp_path}: no manifest.json"]


def test_contamination_between_training_data_and_an_evaluation_set_is_reported():
    clean = ds.overlap(["the quick brown fox jumps"], ["a completely different sentence about invoices"])
    exact = ds.overlap(["Reset my password"], ["reset my password "])
    # 9 of the 10 distinct words are shared: Jaccard 0.9, exactly at the threshold (and 7 of 8 would be 0.875, below it)
    near = ds.overlap(["how do I reset my password in the console today"], ["how do i reset my password in the console"])
    just_below = ds.overlap(["how do I reset my password today please"], ["how do i reset my password today"])

    assert clean["contaminated"] is False
    assert exact["exact"] == 1 and exact["contaminated"] is True
    assert near["near_duplicate_pairs"] == 1 and near["contaminated"] is True
    assert just_below["contaminated"] is False  # the threshold is a line, not a vibe


# ---- public datasets ------------------------------------------------------------------------------------------------------------
def lock(**overrides) -> str:
    entry = {"id": "beir-scifact", "kind": "embedding", "licence": "CC-BY-SA-4.0", "licence_source": "Hub card, read 2026-10-17", "redistribute": False,
             "sources": [{"hub_repo": "BeIR/scifact", "revision": SHA, "files": [{"path": "corpus/c.parquet", "sha256": "0" * 64}]}]}
    entry.update(overrides)
    return json.dumps([entry])


def test_a_valid_lock_entry_parses():
    (entry,) = ds.parse_public_lock(lock())
    assert entry.id == "beir-scifact" and entry.sources[0].hub_repo == "BeIR/scifact" and entry.redistribute is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"licence": "CC-BY-NC-4.0"}, "not on the allow-list"),
        ({"licence": None}, "not on the allow-list"),
        ({"redistribute": True}, "redistribute must be false"),
        ({"sources": []}, "no sources"),
        ({"sources": [{"hub_repo": "x/y", "revision": "main", "files": [{"path": "a", "sha256": "0"}]}]}, "full 40-hex commit sha"),
        ({"sources": [{"hub_repo": "x/y", "revision": SHA, "files": []}]}, "lists no files"),
    ],
)
def test_a_lock_entry_that_breaks_a_rule_is_refused(overrides, message):
    with pytest.raises(DatasetError, match=message):
        ds.parse_public_lock(lock(**overrides))


class FakeHub:
    """Stands in for hf_hub_download: writes `content[filename]` into local_dir/filename."""

    def __init__(self, content: dict[str, bytes], fail_first: int = 0):
        self.content, self.calls, self.fail_first = content, 0, fail_first

    def __call__(self, *, repo_id, filename, repo_type, revision, local_dir, etag_timeout):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("network blip")
        assert repo_type == "dataset" and revision == SHA  # pinned to the commit, not a branch
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content[filename])
        return str(target)


def _entry_for(content: bytes):
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    return ds.parse_public_lock(lock(sources=[{"hub_repo": "BeIR/scifact", "revision": SHA, "files": [{"path": "corpus/c.parquet", "sha256": sha}]}]))[0]


def test_fetch_downloads_verifies_and_then_works_offline_from_the_cache(tmp_path):
    entry, hub = _entry_for(b"corpus bytes"), FakeHub({"corpus/c.parquet": b"corpus bytes"})
    ds.fetch_public(entry, tmp_path, downloader=hub, sleep=lambda s: None)
    assert ds.verify_public_cache(entry, tmp_path) == [] and hub.calls == 1

    ds.fetch_public(entry, tmp_path, downloader=hub, sleep=lambda s: None)  # a second run with the network "cut"
    assert hub.calls == 1


def test_a_download_whose_hash_differs_is_removed_and_refused(tmp_path):
    entry, hub = _entry_for(b"expected"), FakeHub({"corpus/c.parquet": b"tampered"})
    with pytest.raises(DatasetError, match="does not match its pinned sha256"):
        ds.fetch_public(entry, tmp_path, downloader=hub, sleep=lambda s: None)
    assert not ds.cache_path(entry, entry.sources[0], "corpus/c.parquet", tmp_path).exists()  # a bad file is never left to be trusted later


def test_a_transient_failure_is_retried_but_only_a_bounded_number_of_times(tmp_path):
    entry = _entry_for(b"x")
    ds.fetch_public(entry, tmp_path, downloader=FakeHub({"corpus/c.parquet": b"x"}, fail_first=2), retries=2, sleep=lambda s: None)
    with pytest.raises(DatasetError, match="is the network up"):
        ds.fetch_public(entry, tmp_path / "other", downloader=FakeHub({"corpus/c.parquet": b"x"}, fail_first=9), retries=2, sleep=lambda s: None)


def test_a_fetch_that_passes_its_total_deadline_stops(tmp_path):
    entry = _entry_for(b"x")
    ticks = iter([0.0, 5000.0, 5000.0, 5000.0])
    with pytest.raises(DatasetError, match="deadline"):
        ds.fetch_public(entry, tmp_path, downloader=FakeHub({"corpus/c.parquet": b"x"}), deadline_s=1200, clock=lambda: next(ticks), sleep=lambda s: None)


def test_verifying_an_uncached_dataset_names_exactly_what_to_fetch(tmp_path):
    (problem,) = ds.verify_public_cache(_entry_for(b"x"), tmp_path)
    assert "BeIR/scifact/corpus/c.parquet is not cached" in problem and "make model-eval-data" in problem


def test_pinning_hashes_each_file_at_the_given_revision(tmp_path):
    hub = FakeHub({"corpus/c.parquet": b"abc"})
    entry = ds.pin_entry("beir-scifact", "embedding", {"BeIR/scifact": ["corpus/c.parquet"]}, licence="CC-BY-SA-4.0", licence_source="card",
                         revisions={"BeIR/scifact": SHA}, downloader=lambda **kw: hub(etag_timeout=0, **kw), work=tmp_path)
    assert entry["sources"][0]["files"][0]["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"  # sha256("abc")
    assert ds.parse_public_lock(json.dumps([entry]))  # and the result is itself a valid lock


def test_parquet_rows_round_trip():
    pq = pytest.importorskip("pyarrow.parquet")
    import tempfile

    import pyarrow as pa

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.parquet"
        pq.write_table(pa.table({"_id": ["1", "2"], "text": ["a", "b"]}), path)
        assert ds.read_parquet(path) == [{"_id": "1", "text": "a"}, {"_id": "2", "text": "b"}]
