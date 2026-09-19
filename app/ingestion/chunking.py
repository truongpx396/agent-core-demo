"""Parent-child, overlapping-sliding-window chunking for the general-purpose
Ingestor (`app/ingestion/ingestor.py`) — closes the "no general Ingestor" gap
from GRAPH_PATTERNS.md pattern 20.

Small **child** chunks are what gets embedded (dense+sparse) and matched by
`hybrid_search` — precise retrieval needs a focused passage, not a diluted
page. Each child's larger **parent** passage is stored alongside it
(`parent_id`/`parent_text` in the payload, `app/ingestion/ingestor.py`) and
surfaced to the LLM/citations instead, since a bare child fragment often
reads as ambiguous out of context. Standard "small-to-big" retrieval,
riding on the existing hybrid-search path — `hybrid_search` doesn't know
`parent_text` exists; `app/agent/tools.py`'s citation formatting reads it.

Children overlap (`child_overlap` shared chars between consecutive windows)
so a fact straddling a hard chunk boundary is still captured whole by at
least one child, at the cost of some redundant embedding.
"""
import uuid
from dataclasses import dataclass, field

DEFAULT_PARENT_CHARS = 1200
# `parent_text` (not `text`) is injected into the LLM prompt per citation
# (app/agent/tools.py::_display_text), and up to RERANK_TOP_K (5) parents
# can be cited in one turn — doubling this risks the ~2300-2800 token range
# where pattern 13 measured qwen2.5:3b dropping the citation-format
# instruction from its system prompt (pattern 20).
DEFAULT_CHILD_CHARS = 600
DEFAULT_CHILD_OVERLAP = 150


@dataclass
class ParentChunk:
    parent_id: str
    text: str
    children: list[str] = field(default_factory=list)


def _split_into_parents(text: str, parent_chars: int) -> list[str]:
    """Paragraph-aware: greedily packs consecutive `\n\n`-separated
    paragraphs into a parent until the next one would exceed
    `parent_chars`, so boundaries land between paragraphs rather than
    mid-sentence. A single paragraph longer than `parent_chars` (e.g. plain
    text with no blank lines) is hard-split instead."""
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
    child chunks. Returns `[]` for blank/whitespace-only text. Short text
    (fits in one parent) still round-trips through this same path, coming
    out as one `ParentChunk` with one child — no special-casing needed."""
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
