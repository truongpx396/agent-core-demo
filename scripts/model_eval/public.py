"""Readers for the pinned public datasets (see datasets.py for how they are fetched and verified).

Each returns plain Python structures that a runner turns into the same per-item data a repository-own set produces, so a public set and
a repo-own set go through identical metric and report code. Nothing here opens the network: it reads the verified cache.
"""
from dataclasses import dataclass
from pathlib import Path

from scripts.model_eval import datasets as ds


@dataclass(frozen=True)
class BeirData:
    corpus: dict[str, str]  # doc id -> "title. text"
    queries: dict[str, str]  # query id -> text, only those with relevance judgements in the test qrels
    qrels: dict[str, dict[str, int]]  # query id -> {doc id: gain (> 0)}


def _file(entry: ds.PublicEntry, hub_repo: str, suffix: str, root: Path) -> Path:
    for source in entry.sources:
        if source.hub_repo == hub_repo:
            for relpath, _sha in source.files:
                if relpath.endswith(suffix):
                    return ds.cache_path(entry, source, relpath, root)
    raise ds.DatasetError(f"{entry.id}: no {suffix} file pinned for {hub_repo}")


def load_beir(entry: ds.PublicEntry, root: Path = ds.PUBLIC_CACHE) -> BeirData:
    """A BEIR task: corpus and queries from the dataset repo, the TEST relevance judgements from its -qrels repo. Gains are the
    judgement scores (SciFact is binary, NFCorpus graded 0-2); a judgement of 0 is not relevance and is dropped."""
    repo = entry.sources[0].hub_repo
    corpus_rows = ds.read_parquet(_file(entry, repo, "corpus-00000-of-00001.parquet", root))
    query_rows = ds.read_parquet(_file(entry, repo, "queries-00000-of-00001.parquet", root))
    qrels_rows = ds.read_tsv(_file(entry, f"{repo}-qrels", "test.tsv", root))
    qrels: dict[str, dict[str, int]] = {}
    for row in qrels_rows:
        gain = int(row["score"])
        if gain > 0:
            qrels.setdefault(str(row["query-id"]), {})[str(row["corpus-id"])] = gain
    corpus = {str(r["_id"]): f"{r['title']}. {r['text']}".strip(". ") if r.get("title") else str(r["text"]) for r in corpus_rows}
    queries = {str(r["_id"]): str(r["text"]) for r in query_rows if str(r["_id"]) in qrels}
    return BeirData(corpus, queries, qrels)


@dataclass(frozen=True)
class LabelledPrompt:
    id: str
    text: str
    label: int  # 1 attack, 0 benign


def load_prompt_injection(entry: ds.PublicEntry, root: Path = ds.PUBLIC_CACHE) -> list[LabelledPrompt]:
    """deepset/prompt-injections (label 1 = injection) or jackhhao/jailbreak-classification (type 'jailbreak' / 'benign')."""
    repo = entry.sources[0].hub_repo
    if repo == "deepset/prompt-injections":
        path = next(p for p, _ in entry.sources[0].files if "/test-" in p)
        rows = ds.read_parquet(ds.cache_path(entry, entry.sources[0], path, root))
        return [LabelledPrompt(f"{entry.id}-{i}", str(r["text"]), int(r["label"])) for i, r in enumerate(rows)]
    if repo == "jackhhao/jailbreak-classification":
        path = entry.sources[0].files[0][0]
        rows = ds.read_csv(ds.cache_path(entry, entry.sources[0], path, root))
        return [LabelledPrompt(f"{entry.id}-{i}", r["prompt"], 1 if r["type"] == "jailbreak" else 0) for i, r in enumerate(rows)]
    raise ds.DatasetError(f"{entry.id}: no reader for {repo}")
