"""Building the reranker's fixed candidate lists from the retrieval dataset (specs/014 Story 1, scenario 4).

A reranker is graded on lists of a stated size (`LIST_SIZE`, the app's prefetch depth) that do NOT depend on any embedding model: the
question's relevant passages plus the best-ranked WRONG passages under a lexical ranking (the hard negatives a first stage would really
return). Gold is shuffled in by a hash, not placed first, so a reranker cannot score by position. Same qids as the retrieval set, so
dev and test stay disjoint.
"""
import hashlib
import json
from pathlib import Path

from scripts.model_eval import datasets as ds
from scripts.model_eval.lexical import Bm25

LIST_SIZE = 20  # app/core/config.py HYBRID_PREFETCH_LIMIT: candidates reaching the reranker
OUT = ds.DATASETS_ROOT / "rerank-domains"


def build_lists(corpus: list[dict], items: list[dict], k: int = LIST_SIZE) -> list[dict]:
    if len(corpus) < k:
        raise ValueError(f"the corpus has {len(corpus)} chunks, fewer than the list size {k}")
    texts = {c["id"]: c["text"] for c in corpus}
    ids = [c["id"] for c in corpus]
    index = Bm25([c["text"] for c in corpus])
    out = []
    for item in items:
        gold = {cid: int(g) for cid, g in item["relevant"].items() if int(g) > 0}
        chosen = list(gold)
        for j in index.top(item["query"], len(corpus)):  # best-ranked wrong passages first
            if len(chosen) >= k:
                break
            if ids[j] not in gold:
                chosen.append(ids[j])
        order = sorted(chosen, key=lambda cid: hashlib.sha256(f"{item['qid']}|{cid}".encode()).hexdigest())  # gold is not always first
        out.append({"qid": item["qid"], "query": item["query"], "domain": item["domain"], "reviewed": item.get("reviewed", False),
                    "candidates": [{"id": cid, "text": texts[cid], "gain": gold.get(cid, 0)} for cid in order]})
    return out


def write_dataset(source: ds.Manifest, lists: dict[str, list[dict]], *, k: int, root: Path = OUT) -> Path:
    target = root / source.version
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "name": "rerank-domains", "version": source.version, "kind": "reranker", "licence": ds.REPO_INTERNAL,
        "list_size": k, "domains": source.raw.get("domains", []),
        "provenance": {"author": f"derived from {source.name} {source.version}: gold passages plus lexical (BM25) hard negatives, no model involved",
                       "derived_from": {"name": source.name, "version": source.version}},
        "splits": {name: {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(target / f"{name}.jsonl", rows), "items": len(rows)} for name, rows in lists.items()},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def main() -> None:
    import sys

    source = ds.load_verified(sorted((ds.DATASETS_ROOT / "retrieval-domains").glob("v*"))[-1])
    corpus = source.corpus()
    lists = {split: build_lists(corpus, source.split_items(split)) for split in source.raw["splits"]}
    target = write_dataset(source, lists, k=LIST_SIZE)
    sys.stdout.write(f"{sum(len(v) for v in lists.values())} lists of {LIST_SIZE} -> {target}\n")


if __name__ == "__main__":
    main()
