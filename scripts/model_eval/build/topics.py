"""Building the `route-topics` dataset: one sentence per row, labelled with one of the label set's topics or `none` (specs/015 FR-010).

`evals/sources/route-topics/prompts.tsv` is `label<TAB>split<TAB>text`. The split is AUTHORED, not hashed: a hash split of ~150 sentences can
leave a label with one item on a side, and a topic matcher cannot be scored on a topic it never sees. `none` rows are the sentences a
matcher must say "none of these" about: bare acknowledgements, identifiers and keyboard mashing, which is what a follow-up turn looks like.

Authored by the implementing agent from the topics the label set names, so every item is `reviewed: false` until a person reads it
(the reports are `PROVISIONAL` until then, like the other repository sets). They are disjoint from the label set's own examples, which this
module checks and a test pins: a sentence that is also an example would be scored against itself.
"""
import json
from pathlib import Path

from scripts.model_eval import datasets as ds

SOURCES = ds.REPO / "evals" / "sources" / "route-topics" / "prompts.tsv"
LABELSET = ds.REPO / "docker" / "ml-service" / "labelsets" / "route-topics.json"
OUT = ds.DATASETS_ROOT / "route-topics"
NONE = "none"


def read_rows(path: Path = SOURCES) -> list[tuple[str, str, str]]:
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[1] not in ("dev", "test") or not parts[2].strip():
            raise ValueError(f"{path.name} line {number}: expected `label<TAB>dev|test<TAB>text`")
        rows.append((parts[0].strip(), parts[1], parts[2].strip()))
    return rows


def labelset_examples(path: Path = LABELSET) -> dict[str, list[str]]:
    return json.loads(path.read_text())["labels"]


def build_splits(rows: list[tuple[str, str, str]], labels: list[str]) -> dict[str, list[dict]]:
    unknown = {label for label, _split, _text in rows} - set(labels) - {NONE}
    if unknown:
        raise ValueError(f"labels the label set does not have: {sorted(unknown)}")
    splits: dict[str, list[dict]] = {"dev": [], "test": []}
    for number, (label, split, text) in enumerate(rows, start=1):
        splits[split].append({"id": f"top-{number:03d}", "text": text, "label": label, "reviewed": False})
    for name, items in splits.items():
        missing = (set(labels) | {NONE}) - {i["label"] for i in items}
        if missing:
            raise ValueError(f"the {name} split has no item for {sorted(missing)}; a matcher cannot be scored on a topic it never sees")
    return splits


def leaks_into_the_label_set(splits: dict[str, list[dict]], examples: dict[str, list[str]]) -> list[str]:
    """Dataset sentences that are (near-)duplicates of an example in the label set."""
    texts = [t for ts in examples.values() for t in ts]
    out = []
    for items in splits.values():
        eval_texts = [i["text"] for i in items]
        out += [eval_texts[j] for _i, j, _s in ds.near_duplicates(texts, eval_texts)]
    return out


def write_dataset(splits: dict[str, list[dict]], labels: list[str], *, version: str = "v1", root: Path = OUT) -> Path:
    target = root / version
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "name": "route-topics", "version": version, "kind": "topics", "licence": ds.REPO_INTERNAL,
        "labels": sorted(labels), "none_label": NONE,
        "provenance": {"author": "authored by the implementing agent from the topics in docker/ml-service/labelsets/route-topics.json; not yet reviewed by a person",
                       "sources": ["evals/sources/route-topics/prompts.tsv"]},
        "splits": {name: {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(target / f"{name}.jsonl", items), "items": len(items)} for name, items in splits.items()},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def main() -> None:
    import sys

    examples = labelset_examples()
    splits = build_splits(read_rows(), list(examples))
    leaks = leaks_into_the_label_set(splits, examples)
    if leaks:
        raise SystemExit(f"{len(leaks)} dataset sentences are near-duplicates of label-set examples: {leaks[:3]}")
    target = write_dataset(splits, list(examples))
    sys.stdout.write(f"{sum(len(v) for v in splits.values())} items (dev {len(splits['dev'])}, test {len(splits['test'])}) -> {target}\n")


if __name__ == "__main__":
    main()
