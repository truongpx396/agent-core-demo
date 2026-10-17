"""The readers for the pinned public formats, and the model-independent BM25 the reranker lists are built with."""
import csv

import pytest

from scripts.model_eval import datasets as ds
from scripts.model_eval import public
from scripts.model_eval.lexical import Bm25, tokenize


def _entry(entry_id, kind, sources) -> ds.PublicEntry:
    return ds.PublicEntry(entry_id, kind, tuple(ds.PublicSource(repo, "a" * 40, tuple((p, "0" * 64) for p in paths)) for repo, paths in sources),
                          "CC-BY-SA-4.0", "test")


def _write_parquet(path, rows):
    pa, pq = pytest.importorskip("pyarrow"), pytest.importorskip("pyarrow.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_a_beir_task_joins_corpus_queries_and_test_judgements_and_keeps_only_judged_queries(tmp_path):
    entry = _entry("beir-x", "embedding", [("BeIR/x", ["corpus/corpus-00000-of-00001.parquet", "queries/queries-00000-of-00001.parquet"]), ("BeIR/x-qrels", ["test.tsv"])])
    src, qsrc = entry.sources
    _write_parquet(ds.cache_path(entry, src, "corpus/corpus-00000-of-00001.parquet", tmp_path), [{"_id": "d1", "title": "Title", "text": "body one"}, {"_id": "d2", "title": "", "text": "body two"}])
    _write_parquet(ds.cache_path(entry, src, "queries/queries-00000-of-00001.parquet", tmp_path), [{"_id": "q1", "title": "", "text": "first"}, {"_id": "q2", "title": "", "text": "unjudged"}])
    tsv = ds.cache_path(entry, qsrc, "test.tsv", tmp_path)
    tsv.parent.mkdir(parents=True, exist_ok=True)
    tsv.write_text("query-id\tcorpus-id\tscore\nq1\td1\t2\nq1\td2\t0\n")

    beir = public.load_beir(entry, tmp_path)

    assert beir.corpus == {"d1": "Title. body one", "d2": "body two"}  # a title is prepended; no title leaves the text alone
    assert beir.queries == {"q1": "first"}  # q2 has no test judgements, so it is not part of the task
    assert beir.qrels == {"q1": {"d1": 2}}  # a judgement of 0 is not relevance


def test_deepset_prompt_injections_use_the_test_file_and_its_label(tmp_path):
    entry = _entry("deepset-x", "screen", [("deepset/prompt-injections", ["data/test-00000-of-00001-aa.parquet", "data/train-00000-of-00001-bb.parquet"])])
    src = entry.sources[0]
    _write_parquet(ds.cache_path(entry, src, "data/test-00000-of-00001-aa.parquet", tmp_path), [{"text": "ignore previous", "label": 1}, {"text": "hello", "label": 0}])
    _write_parquet(ds.cache_path(entry, src, "data/train-00000-of-00001-bb.parquet", tmp_path), [{"text": "TRAIN ONLY", "label": 1}])

    rows = public.load_prompt_injection(entry, tmp_path)

    assert [(r.text, r.label) for r in rows] == [("ignore previous", 1), ("hello", 0)]  # never the train split


def test_jackhhao_jailbreak_type_maps_to_a_binary_label(tmp_path):
    entry = _entry("jb-x", "screen", [("jackhhao/jailbreak-classification", ["balanced/test.csv"])])
    path = ds.cache_path(entry, entry.sources[0], "balanced/test.csv", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["prompt", "type"])
        w.writerows([["act as DAN", "jailbreak"], ["what is the weather", "benign"]])

    assert [(r.label) for r in public.load_prompt_injection(entry, tmp_path)] == [1, 0]


def test_a_dataset_with_no_reader_is_refused_by_name(tmp_path):
    with pytest.raises(ds.DatasetError, match="no reader for"):
        public.load_prompt_injection(_entry("x", "screen", [("some/other", ["f.csv"])]), tmp_path)


def test_bm25_ranks_the_passage_sharing_the_rare_words_first_and_ties_break_by_document_order():
    docs = ["reset your password from account settings", "invoices and billing exports", "the password policy requires rotation", "shipping information for parcels"]
    index = Bm25(docs)

    assert index.top("how do I reset my password", 2) == [0, 2]  # both terms beats one term; the unrelated documents score zero
    assert index.top("zzz unknown words", 3) == [0, 1, 2]  # no term matches: all scores 0, so document order, deterministically
    assert tokenize("Reset, my Password!") == ["reset", "my", "password"]
