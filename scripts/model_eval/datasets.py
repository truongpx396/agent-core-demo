"""Datasets: the frozen, versioned inputs every comparison stands on (specs/014 FR-009, FR-010, FR-011,
data-model.md sections 2-3). The rules are refusals, each from a way an evaluation lies:
- Every file's sha256 is recomputed on load and a mismatch STOPS the run: a dataset changed under the same
  name would silently invalidate every baseline on it.
- `dev` (tuning) and `test` (reporting) are disjoint by id AND by near-duplicate text (normalised-token
  Jaccard >= 0.9); a leaked question flatters a model.
- `reviewed_share` is COMPUTED from the items, never typed: a person flips `reviewed`, nobody asserts it for
  them.
- A public dataset is pinned by commit and per-file hash, carries a licence from a small allow-list, and is
  never redistributed (the cache is untracked). The repo's own datasets hold no tenant data (FR-039).
- Only `fetch_public` opens the network, with a per-request timeout, bounded retries and a total deadline.
"""
import csv
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DATASETS_ROOT = REPO / "evals" / "datasets"
PUBLIC_CACHE = REPO / "var" / "model_eval" / "public"

ALLOWED_LICENCES = frozenset({"CC-BY-4.0", "CC-BY-3.0", "CC-BY-SA-4.0", "Apache-2.0", "MIT", "ODC-BY"})
# The repository has no LICENSE file, so authored data cannot claim one. It is recorded as what it is.
REPO_INTERNAL = "repository-internal"
# Identifiers the repository's seed data and tests use for tenants and people. Repo-own datasets are authored text, never real traffic
# (FR-039); this catches a copy-paste from a fixture or a log.
TENANT_DENYLIST = ("acme", "other-co", "evil-co", "globex", "make-ingest", "test-user")
NEAR_DUPLICATE_JACCARD = 0.9
_SHA = re.compile(r"^[0-9a-f]{40}$")
# The text field each kind's items carry, for the overlap and tenant checks.
TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "embedding": ("query",),
    "reranker": ("query",),
    "screen": ("text",),
    "classifier": ("text",),
    "route": ("text",),
    "topics": ("text",),
}


class DatasetError(Exception):
    """A dataset failed verification. The run stops; no partial report is written."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> str:
    """Writes rows deterministically (sorted keys, one per line) and returns the sha256, so a manifest can record it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows))
    return sha256_file(path)


def reviewed_share(items: Sequence[dict]) -> float:
    """Share of items a person has reviewed. Computed from the items, never typed into a manifest."""
    return (sum(1 for i in items if i.get("reviewed") is True) / len(items)) if items else 0.0


def _tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if (ta or tb) else 1.0


def near_duplicates(left: Sequence[str], right: Sequence[str], threshold: float = NEAR_DUPLICATE_JACCARD) -> list[tuple[int, int, float]]:
    """(i, j, jaccard) for every pair, one from each side, at or above `threshold`. Quadratic, which is fine for sets of hundreds."""
    out = []
    right_tokens = [_tokens(t) for t in right]
    for i, a in enumerate(left):
        ta = _tokens(a)
        for j, tb in enumerate(right_tokens):
            union = ta | tb
            score = len(ta & tb) / len(union) if union else 1.0
            if score >= threshold:
                out.append((i, j, score))
    return out


@dataclass(frozen=True)
class Manifest:
    root: Path
    raw: dict

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def version(self) -> str:
        return self.raw["version"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    def split_items(self, split: str) -> list[dict]:
        return read_jsonl(self.root / self.raw["splits"][split]["file"])

    def corpus(self) -> list[dict]:
        return read_jsonl(self.root / self.raw["corpus"]["file"])


def load_manifest(root: Path) -> Manifest:
    path = root / "manifest.json"
    if not path.exists():
        raise DatasetError(f"{root}: no manifest.json")
    return Manifest(root, json.loads(path.read_text()))


def verify_dataset(root: Path) -> list[str]:
    """Every problem with the dataset at `root`; empty means it may be used."""
    try:
        manifest = load_manifest(root)
    except DatasetError as exc:
        return [str(exc)]
    raw, problems = manifest.raw, []
    where = f"{raw.get('name', root.name)}/{raw.get('version', '?')}"
    for required in ("name", "version", "kind", "licence", "provenance", "splits"):
        if required not in raw:
            problems.append(f"{where}: manifest has no {required!r}")
    if problems:
        return problems
    if raw["licence"] != REPO_INTERNAL and raw["licence"] not in ALLOWED_LICENCES:
        problems.append(f"{where}: licence {raw['licence']!r} is neither {REPO_INTERNAL!r} nor on the allow-list {sorted(ALLOWED_LICENCES)}")
    if not (raw["provenance"].get("generator") or raw["provenance"].get("author")):
        problems.append(f"{where}: provenance must name a generator or an author")
    files = [(f"split {name}", spec) for name, spec in raw["splits"].items()]
    if raw.get("corpus"):
        files.append(("corpus", raw["corpus"]))
    texts: dict[str, list[str]] = {}
    ids: dict[str, set[str]] = {}
    for label, spec in files:
        path = root / spec["file"]
        if not path.exists():
            problems.append(f"{where}: {label} file {spec['file']} is missing")
            continue
        if sha256_file(path) != spec["sha256"]:
            problems.append(f"{where}: {label} file {spec['file']} does not match its recorded sha256 (changed since it was frozen)")
            continue
        rows = read_jsonl(path)
        expected = spec.get("items", spec.get("chunks"))
        if expected is not None and len(rows) != expected:
            problems.append(f"{where}: {label} has {len(rows)} rows, the manifest says {expected}")
        if label.startswith("split"):
            name = label.split()[1]
            key = "qid" if rows and "qid" in rows[0] else "id"
            ids[name] = {r[key] for r in rows if key in r}
            fields = TEXT_FIELDS.get(raw["kind"], ())
            texts[name] = [r[f] for r in rows for f in fields if f in r]
            for r in rows:
                for f in TEXT_FIELDS.get(raw["kind"], ()):
                    low = str(r.get(f, "")).lower()
                    hit = next((t for t in TENANT_DENYLIST if t in low), None)
                    if hit:
                        problems.append(f"{where}: an item mentions {hit!r}, which looks like tenant data (FR-039)")
                        break
    if "dev" in ids and "test" in ids:
        shared = ids["dev"] & ids["test"]
        if shared:
            problems.append(f"{where}: dev and test share {len(shared)} item ids")
        leaks = near_duplicates(texts["dev"], texts["test"])
        if leaks:
            problems.append(f"{where}: {len(leaks)} dev/test pairs are near-duplicates (Jaccard >= {NEAR_DUPLICATE_JACCARD}); the split leaks")
    return problems


def load_verified(root: Path) -> Manifest:
    problems = verify_dataset(root)
    if problems:
        raise DatasetError("dataset verification failed:\n  " + "\n  ".join(problems))
    return load_manifest(root)


def dataset_summary(manifest: Manifest, split: str = "test") -> dict:
    """The `dataset` block of a report: identity and the computed reviewed share (never item text)."""
    items = manifest.split_items(split)
    return {"name": manifest.name, "version": manifest.version, "split": split, "items": len(items), "reviewed_share": reviewed_share(items)}


def overlap(train_texts: Sequence[str], eval_texts: Sequence[str]) -> dict:
    """Exact and near-duplicate overlap between data a head or threshold was fitted on and the data it is reported on (FR-010)."""
    exact = len({t.strip().lower() for t in train_texts} & {t.strip().lower() for t in eval_texts})
    near = near_duplicates(train_texts, eval_texts)
    return {"exact": exact, "near_duplicate_pairs": len(near), "contaminated": exact > 0 or len(near) > 0}


# ---------------------------------------------------------------------------------------------------------------------------------
# Public datasets: pinned, licensed, fetched on demand into an untracked cache
# ---------------------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicSource:
    hub_repo: str
    revision: str
    files: tuple[tuple[str, str], ...]  # (path in the repo, sha256)


@dataclass(frozen=True)
class PublicEntry:
    id: str
    kind: str
    sources: tuple[PublicSource, ...]
    licence: str
    licence_source: str
    redistribute: bool = False
    notes: str = field(default="", compare=False)


def parse_public_lock(text: str) -> list[PublicEntry]:
    entries = []
    for raw in json.loads(text):
        where = f"public dataset {raw.get('id', '?')!r}"
        if raw.get("licence") not in ALLOWED_LICENCES:
            raise DatasetError(f"{where}: licence {raw.get('licence')!r} is not on the allow-list {sorted(ALLOWED_LICENCES)}; it is refused")
        if raw.get("redistribute") is not False:
            raise DatasetError(f"{where}: redistribute must be false (the cache is untracked and nothing is republished)")
        if not raw.get("sources"):
            raise DatasetError(f"{where}: no sources")
        sources = []
        for s in raw["sources"]:
            if not _SHA.match(s.get("revision", "")):
                raise DatasetError(f"{where}: {s.get('hub_repo')} revision must be a full 40-hex commit sha")
            if not s.get("files"):
                raise DatasetError(f"{where}: {s.get('hub_repo')} lists no files")
            sources.append(PublicSource(s["hub_repo"], s["revision"], tuple((f["path"], f["sha256"]) for f in s["files"])))
        entries.append(
            PublicEntry(raw["id"], raw["kind"], tuple(sources), raw["licence"], raw.get("licence_source", ""), False, raw.get("notes", ""))
        )
    return entries


def load_public_lock(path: Path = DATASETS_ROOT / "public.lock.json") -> list[PublicEntry]:
    return parse_public_lock(path.read_text()) if path.exists() else []


def cache_path(entry: PublicEntry, source: PublicSource, relpath: str, root: Path = PUBLIC_CACHE) -> Path:
    return root / entry.id / source.hub_repo.replace("/", "__") / relpath


def verify_public_cache(entry: PublicEntry, root: Path = PUBLIC_CACHE) -> list[str]:
    """What is missing or changed in the cache for `entry`. Names exactly what to fetch (FR-011), never a partial result."""
    problems = []
    for source in entry.sources:
        for relpath, sha in source.files:
            path = cache_path(entry, source, relpath, root)
            if not path.exists():
                problems.append(f"{entry.id}: {source.hub_repo}/{relpath} is not cached; run `make model-eval-data`")
            elif sha256_file(path) != sha:
                problems.append(f"{entry.id}: {source.hub_repo}/{relpath} does not match its pinned sha256 (the upstream changed, or the cache is corrupt)")
    return problems


Downloader = Callable[..., str]


def fetch_public(
    entry: PublicEntry,
    root: Path = PUBLIC_CACHE,
    *,
    downloader: Downloader | None = None,
    request_timeout_s: float = 30.0,
    retries: int = 2,
    deadline_s: float = 1200.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Download and verify every file of `entry` into the untracked cache. Per-request timeout, at most `retries` retries with backoff,
    and a total deadline (Principle V). Raises DatasetError on a hash mismatch or when the deadline passes."""
    if downloader is None:
        from huggingface_hub import constants, hf_hub_download

        constants.HF_HUB_DOWNLOAD_TIMEOUT = int(request_timeout_s)  # module constant: the knob for the body download
        downloader = hf_hub_download
    started = clock()
    for source in entry.sources:
        for relpath, sha in source.files:
            target = cache_path(entry, source, relpath, root)
            if target.exists() and sha256_file(target) == sha:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(retries + 1):
                if clock() - started > deadline_s:
                    raise DatasetError(f"{entry.id}: fetch exceeded its {deadline_s:.0f} s deadline")
                try:
                    downloader(
                        repo_id=source.hub_repo, filename=relpath, repo_type="dataset", revision=source.revision,
                        local_dir=str(root / entry.id / source.hub_repo.replace("/", "__")), etag_timeout=request_timeout_s,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - any transport failure is retried, then reported by name below
                    if attempt == retries:
                        raise DatasetError(f"{entry.id}: could not fetch {source.hub_repo}/{relpath} ({type(exc).__name__}); is the network up?") from exc
                    sleep(2.0 * (attempt + 1))
            if sha256_file(target) != sha:
                target.unlink()
                raise DatasetError(f"{entry.id}: {source.hub_repo}/{relpath} downloaded but does not match its pinned sha256")


def pin_entry(
    entry_id: str, kind: str, hub_repo_files: dict[str, list[str]], *, licence: str, licence_source: str,
    revisions: dict[str, str], downloader: Downloader, work: Path, notes: str = "",
) -> dict:
    """Build a lock entry by downloading each file at the given revision and hashing it. Used once, by the maintainer, to create or
    refresh `public.lock.json`; the result is reviewed and committed."""
    sources = []
    for hub_repo, paths in hub_repo_files.items():
        files = []
        for relpath in paths:
            local = downloader(repo_id=hub_repo, filename=relpath, repo_type="dataset", revision=revisions[hub_repo], local_dir=str(work / hub_repo.replace("/", "__")))
            files.append({"path": relpath, "sha256": sha256_file(Path(local))})
        sources.append({"hub_repo": hub_repo, "revision": revisions[hub_repo], "files": files})
    return {"id": entry_id, "kind": kind, "sources": sources, "licence": licence, "licence_source": licence_source, "redistribute": False, "notes": notes}


# ---------------------------------------------------------------------------------------------------------------------------------
# Readers for the public formats
# ---------------------------------------------------------------------------------------------------------------------------------


def read_parquet(path: Path) -> list[dict[str, Any]]:
    """Rows of a parquet file. pyarrow is a dev dependency, imported only here, so nothing else needs it."""
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
