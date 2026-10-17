"""Recorded baselines (specs/014 FR-006, data-model.md section 4).

A baseline is the stored result of the model in use today on a specific dataset version. Rules that exist because each is a way a
comparison can quietly lie:
  * IMMUTABLE. Writing over an existing baseline is refused (unless forced): re-recording is a new decision, and the old file stays in
    git history.
  * Quality is hardware-independent; EFFICIENCY is an object keyed by hardware id, so a second machine adds a key instead of replacing
    the first, and a comparison uses a latency number only from the same machine profile.
  * The per-item values are stored beside the headline numbers, so a comparison re-runs the candidate only, never the baseline, and the
    paired bootstrap has what it needs.
  * A baseline from a different dataset (name or version) is REFUSED, not warned about: the numbers would not mean the same thing.
  * `provisional` is true while any evaluation item is unreviewed by a person.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from scripts.model_eval.suites import ItemData

SCHEMA_VERSION = 1


class BaselineExists(Exception):
    pass


class BaselineMismatch(Exception):
    pass


@dataclass
class Baseline:
    record: dict
    data: ItemData
    path: Path

    @property
    def name(self) -> str:
        return self.record["candidate"]

    def efficiency_for(self, hardware_id: str) -> dict | None:
        return self.record.get("efficiency", {}).get(hardware_id)


def stem(candidate: str, dataset_name: str, dataset_version: str) -> str:
    return f"{candidate}@{dataset_name}-{dataset_version}"


def path_for(root: Path, kind: str, candidate: str, dataset_name: str, dataset_version: str) -> Path:
    return root / kind / f"{stem(candidate, dataset_name, dataset_version)}.json"


def _items_path(record_path: Path) -> Path:
    return record_path.with_suffix(".items.json")


def write_baseline(root: Path, record: dict, data: ItemData, *, force: bool = False) -> Path:
    ds = record["dataset"]
    path = path_for(root, record["kind"], record["candidate"], ds["name"], ds["version"])
    if path.exists() and not force:
        raise BaselineExists(f"{path} already exists; a baseline is immutable (re-record with force, and say why in the commit)")
    record = {**record, "schema_version": SCHEMA_VERSION, "provisional": ds.get("reviewed_share", 0.0) < 1.0, "per_query": _items_path(path).name}
    path.parent.mkdir(parents=True, exist_ok=True)
    items_text = json.dumps(data.to_json(), sort_keys=True) + "\n"
    _items_path(path).write_text(items_text)
    record["per_query_sha256"] = hashlib.sha256(items_text.encode()).hexdigest()  # so `verify` notices a hand-edited per-item file
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def merge_efficiency(path: Path, hardware_id: str, efficiency: dict) -> None:
    """A second machine adds its latency numbers under its own hardware id without touching the quality figures or the first machine's."""
    record = json.loads(path.read_text())
    record.setdefault("efficiency", {})[hardware_id] = efficiency
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def read_baseline(path: Path) -> Baseline:
    record = json.loads(path.read_text())
    if record.get("schema_version") != SCHEMA_VERSION:
        raise BaselineMismatch(f"{path}: schema_version {record.get('schema_version')!r} is not understood (this build reads {SCHEMA_VERSION})")
    data = ItemData.from_json(json.loads((path.parent / record["per_query"]).read_text()))
    return Baseline(record, data, path)


def find_baseline(root: Path, kind: str, candidate: str, dataset_name: str, dataset_version: str) -> Baseline | None:
    path = path_for(root, kind, candidate, dataset_name, dataset_version)
    return read_baseline(path) if path.exists() else None


def check_comparable(baseline: Baseline, dataset_name: str, dataset_version: str) -> None:
    ds = baseline.record["dataset"]
    if (ds["name"], ds["version"]) != (dataset_name, dataset_version):
        raise BaselineMismatch(
            f"baseline {baseline.name} was recorded on {ds['name']} {ds['version']}, this run used {dataset_name} {dataset_version}; "
            "they are different evaluations and are not compared"
        )
