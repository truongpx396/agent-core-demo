"""Citation-grounding helpers used exclusively by `check_output`
(`app/agent/graph_routing.py`): which offered citations the answer
actually used (`_used_citations`), which cited markers were invented
(`_ungrounded_claims_count`), and two directional "used without
attributing" checks plus their shared sentence-matching machinery
(`_uncited_citation_matches`, `_likely_uncited_citations`,
`_insert_missing_citation_markers`, `_likely_misattributed_citations`).
Split out of `app/agent/graph_routing.py` for file size (see sibling
splits `graph_loop_guards.py`/`graph_output_guardrails.py`); no behavior
change.
"""
import re

from app.agent.graph_utils import _content_words

_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


def _used_citations(content: str, citations: list[dict]) -> list[dict]:
    """`citations` (every numbered source retrieve_context offered)
    filtered down to the markers the final answer actually used — the
    grounded, cited-answer output (GRAPH_PATTERNS.md pattern 20). Computed
    from the answer text itself, not asserted by the model: a marker the
    model didn't actually write never appears here, regardless of what the
    system prompt asked for."""
    if not citations or not isinstance(content, str):
        return []
    referenced = {int(n) for n in _CITATION_MARKER_RE.findall(content)}
    return [
        c
        for c in citations
        if c["marker"].strip("[]").isdigit() and int(c["marker"].strip("[]")) in referenced
    ]


def _ungrounded_claims_count(content: str, citations: list[dict]) -> int:
    """How many `[n]` markers in `content` don't correspond to any real
    citation offered — the model inventing a source number (SYSTEM_PROMPT
    says never to; GRAPH_PATTERNS.md pattern 39). `0` is a meaningful
    value, not just "no signal." Computed independently from
    `_used_citations` (not derived from it) so a bug in one can't mask a
    bug in the other.
    """
    if not isinstance(content, str) or not content:
        return 0
    referenced = {int(n) for n in _CITATION_MARKER_RE.findall(content)}
    real_markers = {
        int(c["marker"].strip("[]"))
        for c in citations
        if c["marker"].strip("[]").isdigit()
    }
    return len(referenced - real_markers)


# Tuned against real qwen2.5:3b paraphrases with no bracket marker (90%+
# word overlap on 8-14 content words), with margin below that while still
# requiring enough distinctive vocabulary that generic shared domain words
# alone can't trip it. Re-validated after the ratio's denominator changed
# to per-sentence (see _likely_uncited_citations) — live data (Langfuse
# `057e3594`, 2026-09-09): genuine paraphrase sentences scored 0.79-1.00,
# sentences merely sharing generic vocabulary across citations topped out
# at 0.50. 0.6 sits in the gap with margin both ways.
_UNCITED_OVERLAP_RATIO = 0.6


def _uncited_citation_matches(
    content: str, citations: list[dict], used: list[dict]
) -> list[tuple[dict, str]]:
    """For each citation not referenced by marker in `content`, its single
    BEST-matching answer sentence (most shared distinctive vocabulary,
    gated by `_UNCITED_OVERLAP_RATIO`) — paired as `(citation, sentence)`.
    Shared by `_likely_uncited_citations` (which citations) and
    `_insert_missing_citation_markers` (also needs where, to append the
    marker).

    Best match, not first-to-clear-bar, matters for insertion: a generic
    transition sentence shouldn't get the marker instead of the sentence
    that actually reads as the source.
    """
    if not content or not citations:
        return []
    used_markers = {c["marker"] for c in used}
    sentences = _SENTENCE_SPLIT_RE.split(content)
    matches = []
    for citation in citations:
        if citation.get("marker") in used_markers:
            continue
        cite_words = _content_words(citation.get("text", ""))
        if not cite_words:
            continue
        best_sentence = None
        best_ratio = 0.0
        for sentence in sentences:
            sentence_words = _content_words(sentence)
            if len(sentence_words) < _MIN_JUDGED_SENTENCE_WORDS:
                continue
            overlap = sentence_words & cite_words
            ratio = len(overlap) / len(sentence_words)
            if ratio >= _UNCITED_OVERLAP_RATIO and ratio > best_ratio:
                best_ratio = ratio
                best_sentence = sentence
        if best_sentence is not None:
            matches.append((citation, best_sentence))
    return matches


def _likely_uncited_citations(
    content: str, citations: list[dict], used: list[dict]
) -> list[dict]:
    """Citations not referenced by marker in `content` where at least one
    answer SENTENCE shares enough distinctive vocabulary with that
    citation's text to suggest the model drew on it without attributing
    it — a stronger signal than "citations were merely offered"
    (metrics.agent_zero_citations_total). General-knowledge/
    calculator-only answers (allowed uncited by SYSTEM_PROMPT) share no
    such vocabulary, so this stays quiet for those.

    Sentence-level (`overlap / len(sentence_words)`, gated by
    `_MIN_JUDGED_SENTENCE_WORDS`), not the original whole-answer-vs-whole-
    citation ratio — real gap found live (Langfuse `057e3594`,
    2026-09-09): a paraphrase of two ~100-word citations scored only
    23-37% against their full length, nowhere near the 60% bar, because a
    long source's total vocabulary dwarfs what a short faithful
    paraphrase reuses. Per-sentence overlap asks the right question
    regardless of source length.

    Delegates to `_uncited_citation_matches`, also used by check_output to
    auto-insert the missing marker instead of retrying the model — seven
    live prompt-only attempts (including the exact-marker retry feedback)
    all failed to get qwen2.5:3b to add one back.
    """
    return [citation for citation, _sentence in _uncited_citation_matches(content, citations, used)]


def _insert_missing_citation_markers(
    content: str, citations: list[dict], used: list[dict]
) -> tuple[str, list[dict]]:
    """Mechanically appends each `_likely_uncited_citations`-flagged
    citation's `[n]` marker onto the sentence `_uncited_citation_matches`
    identified as its best match, instead of asking the model to redo it —
    live-verified necessary: on Langfuse trace `057e3594` (2026-09-09),
    neither the standard citation reminder, six reworded variants, nor the
    exact retry-feedback message got qwen2.5:3b to add a missing marker;
    all seven attempts reproduced identical uncited prose. Since a
    citation is only ever matched to a specific sentence, insertion here
    is fully deterministic.

    Multiple citations best-matching the same sentence get appended
    together (e.g. '...these safeguards [1][2].'); otherwise each marker
    goes before its sentence's trailing punctuation (matching
    SYSTEM_PROMPT's own example), or at the end if there's no trailing
    .!?. Matches by exact substring position to preserve original
    whitespace outside touched sentences.

    Returns `(possibly-modified content, citations actually inserted)` —
    check_output uses the second value for its own metric and recomputes
    `likely_uncited_citations` against the new content rather than
    assuming this emptied it.
    """
    matches = _uncited_citation_matches(content, citations, used)
    if not matches:
        return content, []

    markers_by_sentence: dict[str, list[str]] = {}
    for citation, sentence in matches:
        markers_by_sentence.setdefault(sentence, []).append(citation["marker"])
    ordered_sentences = sorted(markers_by_sentence, key=content.find)

    parts = []
    cursor = 0
    for sentence in ordered_sentences:
        idx = content.find(sentence, cursor)
        if idx == -1:
            continue  # shouldn't happen — `sentence` came from splitting `content` itself
        parts.append(content[cursor:idx])
        markers_text = "".join(markers_by_sentence[sentence])
        end_match = re.search(r"[.!?]$", sentence)
        if end_match:
            parts.append(sentence[: end_match.start()] + f" {markers_text}" + sentence[end_match.start() :])
        else:
            parts.append(sentence + f" {markers_text}")
        cursor = idx + len(sentence)
    parts.append(content[cursor:])

    fixed_citations = [citation for citation, _sentence in matches]
    return "".join(parts), fixed_citations


# Coarse "good enough" sentence splitter (no NLP dependency), same
# posture as `_content_words`. Splits after ./!/? + whitespace; a marker
# like "[3]" never contains those chars so it stays attached to its
# sentence.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Shared by _likely_uncited_citations and _likely_misattributed_citations
# — below this many words a sentence has too little vocabulary for either
# overlap ratio to mean anything. Judged by the SENTENCE's length, not the
# citation's, so a short source can't inflate the ratio by coincidence
# (see _likely_misattributed_citations for the live bug this caught).
_MIN_JUDGED_SENTENCE_WORDS = 4
# Deliberately looser than _UNCITED_OVERLAP_RATIO's 0.6, in the OPPOSITE
# direction: a LOW ratio here still counts as "supported" — this only
# needs to catch a sentence sharing essentially NOTHING with its marker's
# source, not police close paraphrasing.
_MISATTRIBUTED_OVERLAP_RATIO = 0.25


def _likely_misattributed_citations(
    content: str, citations: list[dict], used: list[dict]
) -> list[dict]:
    """Mirror of `_likely_uncited_citations`: catches a real, in-range
    marker used on a sentence that shares no meaningful vocabulary with
    THAT marker's own source — real bug, found live (Langfuse trace
    `ed435567`): the model cited [3] on every sentence of an answer,
    though [3]'s actual content had nothing to do with it. Neither
    `_ungrounded_claims_count` (marker is real) nor `used_citations`
    (marker genuinely appears) catches this.

    For each `used` marker, every citing sentence is checked against that
    marker's source text; flags only when NONE show meaningful overlap
    AND at least one was long enough to judge. Uses
    `_MIN_JUDGED_SENTENCE_WORDS` on the SENTENCE, not the citation — real
    bug, found live: a one-word memory ("magiclab396") cited on an answer
    went undetected because this used to skip any short CITATION, missing
    the clearest misattribution case there is. `_likely_uncited_citations`
    was later given the same floor for the same reason, in the other
    direction. Only skipped when a source has NO content words at all.
    """
    if not content or not used:
        return []
    sentences = _SENTENCE_SPLIT_RE.split(content)
    flagged = []
    for citation in used:
        marker = citation["marker"]
        cite_words = _content_words(citation.get("text", ""))
        if not cite_words:
            continue
        citing_sentences = [s for s in sentences if marker in s]
        judged = False
        supported = False
        for sentence in citing_sentences:
            sentence_words = _content_words(sentence)
            if len(sentence_words) < _MIN_JUDGED_SENTENCE_WORDS:
                continue
            judged = True
            overlap = sentence_words & cite_words
            if len(overlap) / len(sentence_words) >= _MISATTRIBUTED_OVERLAP_RATIO:
                supported = True
                break
        if judged and not supported:
            flagged.append(citation)
    return flagged
