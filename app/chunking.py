"""Parent-child, overlapping-sliding-window chunking for the general-purpose
Ingestor (`app/ingestor.py`) — closes the "no general Ingestor" gap from
GRAPH_PATTERNS.md pattern 20's retrieval work by giving longer ingested
documents (a file, a URL, a pasted block of text) a real chunking strategy,
not a single oversized point per document.

## Why parent AND child chunks, not one size

A single chunk size is fighting two different jobs with one number:
- **Retrieval precision** wants SMALL chunks — a short, focused passage
  embeds and matches a specific query far more precisely than a page-long
  block where the one relevant sentence is diluted by nine irrelevant ones.
- **Answer quality** wants enough SURROUNDING context for the model to
  actually understand what it retrieved — a 300-character fragment often
  reads as an isolated, ambiguous sentence.

So: only the small **child** chunk is embedded (both dense and sparse —
see `app/qdrant_store.py`'s hybrid search) and is what `hybrid_search`
actually matches against. Its **parent** — the larger passage it came
from — is what's stored alongside it and surfaced to the LLM/citations,
via `parent_id`/`parent_text` in the point's payload (`app/ingestor.py`).
This is the standard "small-to-big" retrieval pattern, applied through
this app's own existing hybrid-search machinery rather than a new
retrieval path — `hybrid_search` doesn't know or care that a payload
happens to carry a `parent_text` field; that's read downstream, by
`app/tools.py`'s citation-formatting functions.

## Why overlapping children, not a hard split

A hard, non-overlapping split can cut the one sentence that answers a
query exactly at a chunk boundary, so neither resulting chunk embeds it
whole. A sliding window with overlap (`child_overlap` characters shared
between consecutive children) means a boundary-straddling fact is still
captured *whole* by at least one child chunk, at the cost of some
redundant embedding.
"""
import uuid
from dataclasses import dataclass, field

DEFAULT_PARENT_CHARS = 1200
DEFAULT_CHILD_CHARS = 300
DEFAULT_CHILD_OVERLAP = 75


@dataclass
class ParentChunk:
    parent_id: str
    text: str
    children: list[str] = field(default_factory=list)


def _split_into_parents(text: str, parent_chars: int) -> list[str]:
    """Paragraph-aware where possible: greedily packs consecutive
    `\n\n`-separated paragraphs into a parent until adding the next one
    would exceed `parent_chars`, so a parent boundary lands between
    paragraphs rather than mid-sentence whenever the source text has
    paragraph structure at all. A single paragraph longer than
    `parent_chars` (e.g. ingested plain text with no blank lines) is
    hard-split instead — there's no better boundary to prefer."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    parents: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if len(para) > parent_chars:
            if current:
                parents.append("\n\n".join(current))
                current, current_len = [], 0
            parents.extend(
                para[i : i + parent_chars] for i in range(0, len(para), parent_chars)
            )
            continue
        added_len = len(para) + (2 if current else 0)  # +2 for the "\n\n" join
        if current and current_len + added_len > parent_chars:
            parents.append("\n\n".join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += added_len
    if current:
        parents.append("\n\n".join(current))
    return parents


def _sliding_window(text: str, child_chars: int, child_overlap: int) -> list[str]:
    """Overlapping fixed-size windows over `text`. A window shorter than
    `child_chars` only occurs for the final one, at the end of `text` —
    every other window is exactly `child_chars` (or `text` itself, if
    `text` doesn't exceed `child_chars` to begin with)."""
    if len(text) <= child_chars:
        return [text]
    step = child_chars - child_overlap
    windows = []
    start = 0
    while start < len(text):
        windows.append(text[start : start + child_chars])
        if start + child_chars >= len(text):
            break
        start += step
    return windows


def chunk_text(
    text: str,
    parent_chars: int = DEFAULT_PARENT_CHARS,
    child_chars: int = DEFAULT_CHILD_CHARS,
    child_overlap: int = DEFAULT_CHILD_OVERLAP,
) -> list[ParentChunk]:
    """Split `text` into parent chunks, each carrying its own overlapping
    child chunks. Returns `[]` for blank/whitespace-only text. A `text`
    short enough to fit one parent (most of this app's own sample docs,
    and most `add_note`/`remember` calls) still round-trips through this
    same code path and comes out as exactly one `ParentChunk` with exactly
    one child — no special-casing needed for "short" input, here or in
    any caller.
    """
    if child_overlap >= child_chars:
        raise ValueError("child_overlap must be smaller than child_chars")

    parents = _split_into_parents(text, parent_chars)
    return [
        ParentChunk(
            parent_id=uuid.uuid4().hex,
            text=parent_text,
            children=_sliding_window(parent_text, child_chars, child_overlap),
        )
        for parent_text in parents
    ]
