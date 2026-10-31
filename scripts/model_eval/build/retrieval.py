"""Building the repo's own retrieval dataset: `evals/datasets/retrieval-domains/<version>/` (specs/014 FR-009,
research.md A3).

Source: authored knowledge-base articles for the four domains (`evals/sources/`), the sample documents and the
skill descriptions, chunked with THE APP'S OWN CHUNKER (child windows of DEFAULT_CHILD_CHARS), because the
shape of a real chunk is part of what is measured (the first benchmark here used synthetic text, understated
chunk length 2.4x and reported a cost per chunk 2.7x too high).

Questions are written by a LOCAL model (offline, nothing leaves the machine) and filtered, because a model
asked for a question about a passage copies the passage:
- a question sharing a long verbatim span with its chunk tests string matching, not meaning, and is dropped;
- duplicates and near-duplicates are dropped, within the set and across the dev/test split (a leak flatters a
  model);
- a question whose content words ALL appear in a second chunk is ambiguous (usually overlap between adjacent
  child windows), so that chunk is also marked relevant and the item is kept as `adversarial`.

Every item is written `reviewed: false`. A person flips that flag; nothing here asserts a review happened, and
a baseline recorded before then is marked provisional.
"""
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.model_eval import datasets as ds
from scripts.model_eval.lexical import tokenize

SOURCES = ds.REPO / "evals" / "sources"
OUT = ds.DATASETS_ROOT / "retrieval-domains"
MAX_VERBATIM_SPAN_CHARS = 40  # a question that shares more than this contiguous with its passage is a copy, not a question
DEV_SHARE_PERCENT = 30
QUESTIONS_PER_CHUNK = 2
BANNED = ("the passage", "the document", "this passage", "this document", "according to the text", "the text above")
_STOP = frozenset("the and for are with that this from have what when how does can will which who your you our their about into than then they them been were was has had not but all any one per each its".split())

PROMPT = """You write evaluation questions for a retrieval system. Below is one passage from an internal knowledge base.

Write {n} different questions that a person who has NOT read the passage might ask, and that this passage answers.
Rules:
- Ask about the facts or procedures in the passage, in your own words. Do not copy more than 4 consecutive words from the passage.
- Each question must make sense on its own: never write "the passage", "the document" or "according to the text".
- Do not include the answer in the question.
Return only JSON of this exact shape: {{"questions": ["...", "..."]}}

Passage:
{passage}
"""


@dataclass(frozen=True)
class Source:
    domain: str
    slug: str
    title: str
    text: str


Generator = Callable[[str], str]  # raw model output for a chunk's text


def load_sources(root: Path = SOURCES) -> list[Source]:
    out = []
    for path in sorted(root.glob("*/*.md")):
        head, _, body = path.read_text().partition("\n---\n")
        meta = dict(line.split(": ", 1) for line in head.removeprefix("---\n").splitlines() if ": " in line)
        out.append(Source(path.parent.name, path.stem, meta.get("title", path.stem), body.strip()))
    return out


def extra_sources() -> list[Source]:
    """The sample documents and the skill catalog: real content already in the repository, tagged `general` or with the skill's domain."""
    from app.agent.tools import skill_catalog
    from scripts.sample_docs import DOCS

    out = [Source("general", f"sample-{d['id']}", d["title"], d["text"]) for d in DOCS]
    skill_catalog.reload_skills()
    for name, record in sorted(skill_catalog.get_skills().items()):
        domain = record.domains[0] if record.domains else "general"
        out.append(Source(domain, f"skill-{name}", name, f"{name}: {record.description}"))
    return out


def make_corpus(sources: list[Source]) -> list[dict]:
    """One corpus row per child chunk, with a deterministic id (`<domain>/<slug>#<n>`): the chunker's own parent ids are random."""
    from app.ingestion.chunking import chunk_text

    rows = []
    for src in sources:
        n = 0
        for parent in chunk_text(f"{src.title}\n\n{src.text}"):
            for child in parent.children:
                rows.append({"id": f"{src.domain}/{src.slug}#{n}", "domain": src.domain, "text": child, "source": f"{src.domain}/{src.slug}"})
                n += 1
    return rows


def longest_common_substring(a: str, b: str) -> int:
    """Length of the longest contiguous run shared by the two strings (case-insensitive). Quadratic; strings here are a few hundred chars."""
    a, b = a.lower(), b.lower()
    best, prev = 0, [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, start=1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def content_words(text: str) -> set[str]:
    return {w for w in tokenize(text) if len(w) > 3 and w not in _STOP}


def parse_questions(raw: str, key: str = "questions") -> list[str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)  # tolerate a model wrapping the JSON in prose
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    questions = data.get(key, []) if isinstance(data, dict) else []
    if not isinstance(questions, list):  # a string here would be iterated character by character
        return []
    return [q.strip() for q in questions if isinstance(q, str) and q.strip()]


def acceptable(question: str, chunk_text_: str) -> bool:
    if not 20 <= len(question) <= 220:
        return False
    low = question.lower()
    if any(b in low for b in BANNED):
        return False
    return longest_common_substring(question, chunk_text_) <= MAX_VERBATIM_SPAN_CHARS


def split_of(chunk_id: str) -> str:
    """Deterministic: every question about one chunk lands in the same split, so a chunk never straddles dev and test."""
    return "dev" if int(hashlib.sha256(chunk_id.encode()).hexdigest()[:8], 16) % 100 < DEV_SHARE_PERCENT else "test"


def build_items(corpus: list[dict], generate: Generator) -> tuple[list[dict], dict]:
    """Generate, filter and label. Returns (items, stats). `generate(prompt) -> raw model output`."""
    texts = {c["id"]: c["text"] for c in corpus}
    cover = {cid: content_words(text) for cid, text in texts.items()}
    items: list[dict] = []
    kept_texts: list[str] = []
    stats = {"generated": 0, "copied": 0, "duplicate": 0, "malformed": 0}
    for chunk in corpus:
        questions = parse_questions(generate(PROMPT.format(n=QUESTIONS_PER_CHUNK, passage=chunk["text"])))
        if not questions:
            stats["malformed"] += 1
        taken = 0
        for q in questions:
            stats["generated"] += 1
            if not acceptable(q, chunk["text"]):
                stats["copied"] += 1
                continue
            if ds.near_duplicates([q], kept_texts):
                stats["duplicate"] += 1
                continue
            wanted = content_words(q)
            others = [cid for cid, words in cover.items() if cid != chunk["id"] and wanted and len(wanted & words) / len(wanted) >= 0.9]
            relevant = {chunk["id"]: 1, **{cid: 1 for cid in others}}
            items.append({"qid": f"{chunk['id']}.q{taken}", "domain": chunk["domain"], "query": q, "relevant": relevant,
                          "kind": "adversarial" if others else "direct", "reviewed": False})
            kept_texts.append(q)
            taken += 1
    return items, stats


def split_items(items: list[dict]) -> dict[str, list[dict]]:
    """dev/test by source chunk; any test question that is a near-duplicate of a dev question is dropped, not moved."""
    out: dict[str, list[dict]] = {"dev": [], "test": []}
    for item in items:
        out[split_of(item["qid"].rsplit(".q", 1)[0])].append(item)
    leaks = {j for _i, j, _s in ds.near_duplicates([i["query"] for i in out["dev"]], [i["query"] for i in out["test"]])}
    out["test"] = [it for j, it in enumerate(out["test"]) if j not in leaks]
    return out


def write_dataset(corpus: list[dict], splits: dict[str, list[dict]], *, generator: str, prompt: str, generated_at: str, version: str = "v1", root: Path = OUT) -> Path:
    target = root / version
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "name": "retrieval-domains",
        "version": version,
        "kind": "embedding",
        "licence": ds.REPO_INTERNAL,
        "domains": sorted({c["domain"] for c in corpus}),
        "provenance": {
            "generator": generator,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "generated_at": generated_at,
            "filters": {"max_verbatim_span_chars": MAX_VERBATIM_SPAN_CHARS, "dedupe": True, "dev_share_percent": DEV_SHARE_PERCENT,
                        "questions_per_chunk": QUESTIONS_PER_CHUNK},
            "note": "questions are model-written and NOT yet reviewed by a person; reviewed_share is computed from the items",
        },
        "splits": {name: {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(target / f"{name}.jsonl", rows), "items": len(rows)} for name, rows in splits.items()},
        "corpus": {"file": "corpus.jsonl", "sha256": ds.write_jsonl(target / "corpus.jsonl", corpus), "chunks": len(corpus)},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def ollama_generator(model: str = "qwen2.5:7b", base: str = "http://localhost:11434", seed: int = 20261017, timeout: float = 180.0) -> Generator:
    """The local model, JSON-constrained and seeded so a re-run is as reproducible as the model allows. A hung call is bounded."""
    import httpx

    def generate(prompt: str) -> str:
        response = httpx.post(
            f"{base}/api/chat", timeout=timeout,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False, "format": "json",
                  "options": {"temperature": 0.3, "seed": seed, "num_predict": 300}},
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    return generate


def main() -> None:
    from app.bootstrap import wire

    wire()
    import sys
    from datetime import UTC, datetime

    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b"
    corpus = make_corpus(load_sources() + extra_sources())
    items, stats = build_items(corpus, ollama_generator(model))
    splits = split_items(items)
    target = write_dataset(corpus, splits, generator=f"ollama/{model}", prompt=PROMPT, generated_at=datetime.now(UTC).date().isoformat())
    sys.stdout.write(f"{len(corpus)} chunks, {len(items)} questions kept ({stats}); dev {len(splits['dev'])}, test {len(splits['test'])} -> {target}\n")


if __name__ == "__main__":
    main()
