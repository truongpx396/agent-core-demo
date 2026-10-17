"""Declaring a candidate model (specs/014 FR-007, data-model.md section 1).

Adding a model to be evaluated means adding a `[[candidate]]` table to `evals/candidates/<kind>.toml`, never editing metric, dataset or
report code. The rules below are refusals at load time: a candidate with no licence, or an in-process one whose revision is a branch
name (which would silently change under it), cannot even be listed.
"""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("embedding", "reranker", "screen", "classifier")
PATHS = ("gateway", "service", "inprocess")
POOLINGS = ("cls", "mean", "last", "none")
_NAME = re.compile(r"^[a-z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# A model's own context limit is not knowable offline, so this only catches a typo (a value no encoder in this family has).
MAX_PLAUSIBLE_LENGTH = 131072


@dataclass(frozen=True)
class Source:
    repo: str | None = None
    file: str | None = None
    revision: str | None = None
    alias: str | None = None  # gateway: the LiteLLM alias
    model: str | None = None  # service: the name the ML service serves it under


@dataclass(frozen=True)
class Prepare:
    query_prefix: str = ""
    document_prefix: str = ""
    pooling: str = "cls"
    normalize: bool = True
    max_length: int = 512
    pad_token: str | None = None


@dataclass(frozen=True)
class Candidate:
    name: str
    kind: str
    path: str
    licence: str
    source: Source = field(default_factory=Source)
    prepare: Prepare = field(default_factory=Prepare)
    notes: str = ""
    baseline: bool = False  # the model in use today for this kind: what every comparison is made against


def parse_candidates(text: str, *, expected_kind: str | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for entry in tomllib.loads(text).get("candidate", []):
        where = f"candidate {entry.get('name', '?')!r}"
        name = str(entry.get("name", ""))
        if not _NAME.match(name):
            raise ValueError(f"{where}: name must match [a-z0-9._-]+")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise ValueError(f"{where}: kind must be one of {KINDS}")
        if expected_kind is not None and kind != expected_kind:
            raise ValueError(f"{where}: kind {kind!r} in a file for {expected_kind!r}")
        if name in seen:
            raise ValueError(f"{where}: duplicate name within the kind")
        seen.add(name)
        path = entry.get("path")
        if path not in PATHS:
            raise ValueError(f"{where}: path must be one of {PATHS}")
        licence = str(entry.get("licence", "")).strip()
        if not licence:
            raise ValueError(f"{where}: licence is required; a candidate with none is refused")
        src_raw, prep_raw = entry.get("source", {}), entry.get("prepare", {})
        source = Source(**{k: src_raw.get(k) for k in ("repo", "file", "revision", "alias", "model")})
        if path == "inprocess":
            if not (source.repo and source.file and source.revision):
                raise ValueError(f"{where}: an inprocess candidate needs source.repo, source.file and source.revision")
            if not _SHA.match(source.revision):
                raise ValueError(f"{where}: source.revision must be a full 40-hex commit sha, never a branch or a short sha")
        if path == "gateway" and not source.alias:
            raise ValueError(f"{where}: a gateway candidate needs source.alias")
        if path == "service" and not source.model:
            raise ValueError(f"{where}: a service candidate needs source.model")
        prepare = Prepare(
            query_prefix=str(prep_raw.get("query_prefix", "")),
            document_prefix=str(prep_raw.get("document_prefix", "")),
            pooling=str(prep_raw.get("pooling", "cls")),
            normalize=bool(prep_raw.get("normalize", True)),
            max_length=int(prep_raw.get("max_length", 512)),
            pad_token=prep_raw.get("pad_token"),
        )
        if prepare.pooling not in POOLINGS:
            raise ValueError(f"{where}: prepare.pooling must be one of {POOLINGS}")
        if not 1 <= prepare.max_length <= MAX_PLAUSIBLE_LENGTH:
            raise ValueError(f"{where}: prepare.max_length {prepare.max_length} is not a plausible context length")
        out.append(Candidate(name, kind, path, licence, source, prepare, str(entry.get("notes", "")), bool(entry.get("baseline", False))))
    if sum(c.baseline for c in out) > 1:
        raise ValueError("more than one candidate is marked baseline = true; a kind has exactly one model in use")
    return out


def load_candidates(path: Path, *, expected_kind: str | None = None) -> list[Candidate]:
    return parse_candidates(path.read_text(), expected_kind=expected_kind) if path.exists() else []
