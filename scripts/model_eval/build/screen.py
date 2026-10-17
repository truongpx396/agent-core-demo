"""Building the prompt-injection screening dataset from authored attacks and benign prompts (specs/014 Story 1, scenario 5).

`evals/sources/screen/attacks.tsv` is `technique<TAB>text` (ignore-instructions, role-play, exfiltration, indirect injection through a
document, authority claims, tool abuse, obfuscation, multi-step, emotional pressure, privilege escalation). `benign.tsv` is
`domain<TAB>text`: ordinary questions in the four domains, INCLUDING hard negatives that merely talk about instructions, policies,
overrides and system prompts, because a screen that blocks those hurts real users. Authored by the implementing agent, so it is
`reviewed: false` until a person reads it.
"""
import hashlib
import json
from pathlib import Path

from scripts.model_eval import datasets as ds

SOURCES = ds.REPO / "evals" / "sources" / "screen"
OUT = ds.DATASETS_ROOT / "screen-domains"
DEV_SHARE_PERCENT = 30


def read_tsv(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            tag, text = line.split("\t", 1)
            rows.append((tag.strip(), text.strip()))
    return rows


def build_items(sources: Path = SOURCES) -> list[dict]:
    items = []
    for n, (technique, text) in enumerate(read_tsv(sources / "attacks.tsv"), start=1):
        items.append({"id": f"atk-{n:03d}", "text": text, "label": "attack", "technique": technique, "domain": "any", "reviewed": False})
    for n, (domain, text) in enumerate(read_tsv(sources / "benign.tsv"), start=1):
        items.append({"id": f"ben-{n:03d}", "text": text, "label": "benign", "domain": domain, "reviewed": False})
    return items


def split(items: list[dict]) -> dict[str, list[dict]]:
    """By id hash, so a re-run never moves an item; a test item that is a near-duplicate of a dev one is dropped rather than moved."""
    out: dict[str, list[dict]] = {"dev": [], "test": []}
    for it in items:
        out["dev" if int(hashlib.sha256(it["id"].encode()).hexdigest()[:8], 16) % 100 < DEV_SHARE_PERCENT else "test"].append(it)
    leaks = {j for _i, j, _s in ds.near_duplicates([i["text"] for i in out["dev"]], [i["text"] for i in out["test"]])}
    out["test"] = [it for j, it in enumerate(out["test"]) if j not in leaks]
    for name, rows in out.items():
        if {r["label"] for r in rows} != {"attack", "benign"}:
            raise ValueError(f"the {name} split lacks one of the two classes; a screen cannot be scored on it")
    return out


def write_dataset(splits: dict[str, list[dict]], *, version: str = "v1", root: Path = OUT) -> Path:
    target = root / version
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "name": "screen-domains", "version": version, "kind": "screen", "licence": ds.REPO_INTERNAL,
        "domains": sorted({i["domain"] for rows in splits.values() for i in rows}),
        "provenance": {"author": "authored by the implementing agent from the failure modes in specs/014; not yet reviewed by a person",
                       "sources": ["evals/sources/screen/attacks.tsv", "evals/sources/screen/benign.tsv"]},
        "splits": {name: {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(target / f"{name}.jsonl", rows), "items": len(rows)} for name, rows in splits.items()},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def main() -> None:
    import sys

    splits = split(build_items())
    target = write_dataset(splits)
    sys.stdout.write(f"{sum(len(v) for v in splits.values())} items (dev {len(splits['dev'])}, test {len(splits['test'])}) -> {target}\n")


if __name__ == "__main__":
    main()
