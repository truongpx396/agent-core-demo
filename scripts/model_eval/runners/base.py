"""Pieces every runner shares: the error that means "a prerequisite is missing", timing, and dataset resolution."""
import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval.routing import DEFAULT_MARGIN


class PrerequisiteMissing(Exception):
    """A model, dataset or service the run needs is not available. The CLI exits 3 and writes NO report (FR-011): a partial report
    presented as complete is worse than none."""


@dataclass
class RunSettings:
    split: str = "test"
    runs: int = 3  # repeats of the latency loop; the percentiles are over all samples
    seed: int = 20261017
    threads: int | None = None
    dataset: str | None = None  # a repo-own dataset name or a public lock id; None = the kind's default
    dataset_root: Path = ds.DATASETS_ROOT
    public_root: Path = ds.PUBLIC_CACHE
    public_lock: Path = ds.DATASETS_ROOT / "public.lock.json"
    list_size: int | None = None  # reranker candidate-list size for public sets (the repo-own sets fix theirs)
    outcomes: Path | None = None  # classifier: the labelled outcomes file (routing.py); None = the one file in the dataset's outcomes/ directory
    margin: float = DEFAULT_MARGIN  # classifier: strong - local must reach this for a prompt to count as needing the stronger model
    head_dir: Path = ds.REPO / "var" / "model_eval" / "heads"  # classifier, in-process: where `make router-head-train` wrote the head files


def percentiles(samples_ms: Sequence[float]) -> dict[str, float]:
    arr = np.asarray(samples_ms, dtype=float)
    return {"p50": float(np.percentile(arr, 50)), "p95": float(np.percentile(arr, 95))}


def time_ms(fn: Callable[[], object]) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def seeded_rng(seed: int, text: str) -> np.random.Generator:
    """Deterministic per (seed, text): the same text always gets the same random output, so a control is reproducible."""
    return np.random.default_rng(int(hashlib.sha256(f"{seed}:{text}".encode()).hexdigest()[:8], 16))


def latest_version(root: Path, name: str) -> Path:
    versions = sorted(p for p in (root / name).glob("v*") if p.is_dir()) if (root / name).exists() else []
    if not versions:
        raise PrerequisiteMissing(f"dataset {name!r} has no version under {root}; build it with `python -m scripts.model_eval build {name}`")
    return versions[-1]


def public_entry(settings: RunSettings, entry_id: str) -> ds.PublicEntry:
    entry = next((e for e in ds.load_public_lock(settings.public_lock) if e.id == entry_id), None)
    if entry is None:
        raise PrerequisiteMissing(f"{entry_id!r} is not in {settings.public_lock}")
    problems = ds.verify_public_cache(entry, settings.public_root)
    if problems:
        raise PrerequisiteMissing("\n".join(problems))
    return entry


def public_block(entry: ds.PublicEntry) -> dict:
    return {"id": entry.id, "revision": entry.sources[0].revision, "licence": entry.licence}
