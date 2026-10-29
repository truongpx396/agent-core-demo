"""Citation-grounding helpers used exclusively by `check_output` (`app/agent/graph/routing.py`) and
the registry's two citation checks (`graph/output_checks.py`): which offered citations the answer
actually used (`find_used_citations`), which cited markers were invented
(`count_ungrounded_claims`), and two directional "used without attributing" checks plus their shared
sentence-matching machinery (`_uncited_citation_matches`, `find_likely_uncited`,
`insert_missing_citation_markers`, `find_likely_misattributed`).
"""
import re

from app.agent.graph.messages import content_words, split_sentences

_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


def find_used_citations(content: str, citations: list[dict]) -> list[dict]:
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


def count_ungrounded_claims(content: str, citations: list[dict]) -> int:
    """How many `[n]` markers in `content` don't correspond to any real
    citation offered — the model inventing a source number (SYSTEM_PROMPT
    says never to; GRAPH_PATTERNS.md pattern 39). `0` is a meaningful
    value, not just "no signal." Computed independently from
    `find_used_citations` (not derived from it) so a bug in one can't mask a
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
# to per-sentence (see find_likely_uncited) — live data (Langfuse
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
    Shared by `find_likely_uncited` (which citations) and
    `insert_missing_citation_markers` (also needs where, to append the
    marker).

    Best match, not first-to-clear-bar, matters for insertion: a generic
    transition sentence shouldn't get the marker instead of the sentence
    that actually reads as the source.
    """
    if not content or not citations:
        return []
    used_markers = {c["marker"] for c in used}
    sentences = split_sentences(content)
    matches = []
    for citation in citations:
        if citation.get("marker") in used_markers:
            continue
        cite_words = content_words(citation.get("text", ""))
        if not cite_words:
            continue
        best_sentence = None
        best_ratio = 0.0
        for sentence in sentences:
            sentence_words = content_words(sentence)
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


def find_likely_uncited(
    content: str, citations: list[dict], used: list[dict]
) -> list[dict]:
    """Citations offered but never referenced by marker in `content`, where at least one answer SENTENCE shares
    enough distinctive vocabulary with the citation's text to suggest the model drew on it without attributing
    it: a stronger signal than "citations were merely offered" (metrics.agent_zero_citations_total).
    General-knowledge and calculator-only answers (allowed uncited by SYSTEM_PROMPT) share none, so this stays
    quiet for them.

    Judged per sentence (`overlap / len(sentence_words)`, gated by `_MIN_JUDGED_SENTENCE_WORDS`), not whole
    answer against whole citation. Found live (Langfuse `057e3594`, 2026-09-09): a paraphrase of two ~100-word
    citations scored 23-37% against their full length, far below the 60% bar, because a long source's
    vocabulary dwarfs what a short faithful paraphrase reuses.

    Delegates to `_uncited_citation_matches`, also used by check_output to insert the missing marker instead
    of retrying: seven prompt-only attempts (including the exact-marker retry feedback) never got qwen2.5:3b
    to add one back.
    """
    return [citation for citation, _sentence in _uncited_citation_matches(content, citations, used)]


def insert_missing_citation_markers(
    content: str, citations: list[dict], used: list[dict]
) -> tuple[str, list[dict]]:
    """Mechanically append each flagged citation's `[n]` marker to the sentence `_uncited_citation_matches`
    matched it to, instead of asking the model to redo it. Live-verified necessary: on Langfuse trace
    `057e3594` (2026-09-09) neither the standard reminder, six rewordings, nor the exact retry feedback got
    qwen2.5:3b to add a missing marker (seven attempts, identical uncited prose). A citation is only ever
    matched to a specific sentence, so insertion is deterministic.

    Several citations best-matching one sentence are appended together ('...these safeguards [1][2].');
    otherwise each marker goes before the sentence's trailing punctuation (as SYSTEM_PROMPT's own example
    does), or at the end if there is none. Matching is by exact substring position, preserving whitespace
    outside touched sentences.

    Returns `(possibly-modified content, citations actually inserted)`; check_output uses the second for its
    metric and recomputes the "uncited" finding on the new content rather than assuming this emptied it.
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


# Shared by find_likely_uncited and find_likely_misattributed
# — below this many words a sentence has too little vocabulary for either
# overlap ratio to mean anything. Judged by the SENTENCE's length, not the
# citation's, so a short source can't inflate the ratio by coincidence
# (see find_likely_misattributed for the live bug this caught).
_MIN_JUDGED_SENTENCE_WORDS = 4
# Deliberately looser than _UNCITED_OVERLAP_RATIO's 0.6, in the OPPOSITE
# direction: a LOW ratio here still counts as "supported" — this only
# needs to catch a sentence sharing essentially NOTHING with its marker's
# source, not police close paraphrasing.
_MISATTRIBUTED_OVERLAP_RATIO = 0.25


def find_likely_misattributed(
    content: str, citations: list[dict], used: list[dict]
) -> list[dict]:
    """Mirror of `find_likely_uncited`: a real, in-range marker used on a sentence that shares no meaningful
    vocabulary with THAT marker's source. Found live (Langfuse `ed435567`): the model cited [3] on every
    sentence though [3]'s content had nothing to do with it. Neither `count_ungrounded_claims` (the marker is
    real) nor `used_citations` (it genuinely appears) catches this.

    For each `used` marker, every citing sentence is checked against that marker's source; it flags only when
    NONE overlap meaningfully AND at least one was long enough to judge. `_MIN_JUDGED_SENTENCE_WORDS` applies
    to the SENTENCE, not the citation: a one-word memory ("magiclab396") cited on an answer went undetected
    when short CITATIONS were skipped, missing the clearest misattribution there is (`find_likely_uncited`
    later got the same floor for the same reason, in the other direction). Only a source with NO content words
    is skipped.

    Sentences come from `messages.split_sentences`, which keeps a marker written after the full stop ("... are
    free. [1]") with the sentence it follows; splitting on the full stop alone judged that `[1]` against the
    NEXT sentence and flagged a correct citation (Langfuse `1e46b2c8`).
    """
    if not content or not used:
        return []
    sentences = split_sentences(content)
    flagged = []
    for citation in used:
        marker = citation["marker"]
        cite_words = content_words(citation.get("text", ""))
        if not cite_words:
            continue
        citing_sentences = [s for s in sentences if marker in s]
        judged = False
        supported = False
        for sentence in citing_sentences:
            sentence_words = content_words(sentence)
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
