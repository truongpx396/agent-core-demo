"""Citation-grounding helpers used exclusively by `check_output`
(`app/agent/graph_routing.py`): which offered citations the final answer
actually used (`_used_citations`), which cited markers were invented
(`_ungrounded_claims_count`), and the two directional "did the model draw
on this source without attributing it" checks and their shared
sentence-matching machinery (`_uncited_citation_matches`,
`_likely_uncited_citations`, `_insert_missing_citation_markers`,
`_likely_misattributed_citations`). Split out of
`app/agent/graph_routing.py` purely for file size — see that module's own
docstring, and `app/agent/graph_loop_guards.py`/
`app/agent/graph_output_guardrails.py` for the sibling splits. No behavior
change from the pre-split single-file version.
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
    """How many `[n]` markers the answer text references that do NOT
    correspond to any real citation `retrieve_context` actually offered —
    the model inventing a source number, which `SYSTEM_PROMPT` explicitly
    tells it never to do (GRAPH_PATTERNS.md pattern 39). A structural
    field on every run, not just a debug-only signal: `0` is itself a
    meaningful, valid value ("no hallucinated citations this turn"), the
    same way `source_count == 0` is a valid state elsewhere in this app,
    never an error. Deliberately the mirror image of `_used_citations` —
    that function answers "which real citations got used," this one
    answers "which referenced markers weren't real" — computed
    independently rather than derived from one another so a bug in one
    can't silently mask a bug in the other.
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


# Originally tuned (and still valid) against two real qwen2.5:3b answers
# that paraphrased a source near-verbatim without any bracket marker (see
# the conversation this was added from): both cleared 90%+ overlap on
# 8-14 content words. 0.6 leaves real margin below that while still
# requiring enough distinctive vocabulary that a coincidental match on a
# handful of common domain words ("Qdrant", "search") alone can't trip it.
#
# The ratio's DENOMINATOR changed since (see _likely_uncited_citations's
# own docstring for why: overlap / len(cite_words) structurally couldn't
# catch a genuine paraphrase of a long source), but 0.6 itself is still
# the right cutoff under the new overlap / len(sentence_words) — live
# numbers from the real trace this was re-tuned against (Langfuse
# `057e3594`, 2026-09-09): sentences that were clearly direct restatements
# of one specific citation scored 0.79-1.00; sentences merely sharing
# generic connective/domain vocabulary across MULTIPLE citations (a real
# risk in a corpus this deliberately overlapping — everything's from one
# "AI agents" book) topped out at 0.50. 0.6 sits in the gap between those
# two groups with margin on both sides, not a guess.
_UNCITED_OVERLAP_RATIO = 0.6


def _uncited_citation_matches(
    content: str, citations: list[dict], used: list[dict]
) -> list[tuple[dict, str]]:
    """For each citation NOT referenced by marker in `content` (i.e. not in
    `used`, `_used_citations`'s own output), its single BEST-matching
    answer sentence — the one sharing the most distinctive vocabulary with
    that citation's own text, only when it clears `_UNCITED_OVERLAP_RATIO`
    — paired together as `(citation, sentence)`. The shared sentence-
    matching logic behind both `_likely_uncited_citations` (just wants to
    know WHICH citations) and `_insert_missing_citation_markers` (also
    needs to know WHERE, so it can append the marker to that exact
    sentence). See `_likely_uncited_citations`'s own docstring for why
    this is sentence-level in the first place, not the original
    whole-answer-vs-whole-citation ratio.

    The BEST match, not just the first sentence to clear the bar — matters
    for insertion specifically: attaching a citation's marker to whichever
    sentence happens to appear first (a plausible-but-generic transition
    sentence, say) instead of the one that actually reads as its source
    would put the marker somewhere a reader has no reason to trust it.
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
    """Citations NOT referenced by marker in `content` where at least one
    ANSWER SENTENCE shares enough distinctive vocabulary with that
    citation's own text to suggest the model drew on it anyway without
    attributing it — a much stronger, less ambiguous signal than
    "citations were merely available" (metrics.agent_zero_citations_total's
    own, noisier trigger in check_output): a general-knowledge or
    calculator-only answer (both explicitly allowed uncited by
    SYSTEM_PROMPT) has no particular reason to share heavy vocabulary with
    an unrelated fetched document, so this stays quiet for those, unlike
    the plain zero-citations check.

    Sentence-level, mirroring `_likely_misattributed_citations`'s own
    ratio (`overlap / len(sentence_words)`, gated by the same
    `_MIN_JUDGED_SENTENCE_WORDS` floor on how much of the SENTENCE,
    not the source, there is to judge) rather than the ORIGINAL whole-
    answer-vs-whole-citation ratio this function used before — real gap,
    found live via Langfuse (trace `057e3594`, 2026-09-09): a qwen2.5:3b
    answer paraphrased two ~100-word citations into a few short sentences,
    restating enough of each to be unmistakably the source (see this
    file's own regression tests for the exact text), with zero bracket
    markers — but scored only 23-37% overlap against each citation's FULL
    word count, nowhere near the (then-) 60% bar. Measuring overlap
    against the CITATION's length structurally punishes exactly this
    case: a long source's total vocabulary will always dwarf what a short,
    faithful paraphrase of it actually reuses, no matter how directly that
    paraphrase is drawn from it. Measuring per-sentence against the
    SENTENCE's own length instead asks the right question — "how much of
    what the model chose to write in THIS sentence came from THIS
    source" — which stays high for a real, focused paraphrase regardless
    of how long the source it's drawn from happens to be.

    Delegates the actual matching to `_uncited_citation_matches`, which
    `check_output` also uses to auto-insert the missing marker instead of
    retrying the model over it — see `_insert_missing_citation_markers`'s
    own docstring for why: seven different live prompt-level attempts
    (the original reminder, six reworded variants, and the actual
    concrete retry-feedback message naming the exact missed marker) all
    failed to get qwen2.5:3b to add one back on a real case.
    """
    return [citation for citation, _sentence in _uncited_citation_matches(content, citations, used)]


def _insert_missing_citation_markers(
    content: str, citations: list[dict], used: list[dict]
) -> tuple[str, list[dict]]:
    """Mechanically append each `_likely_uncited_citations`-flagged
    citation's `[n]` marker onto the specific sentence
    `_uncited_citation_matches` identified as its best match, instead of
    asking the model to redo it. Live-verified this matters, not assumed:
    on a real Langfuse trace (`057e3594`, 2026-09-09) where the model
    paraphrased two sources with zero markers, neither the standard
    citation reminder, six reworded variants of it (including one with a
    worked example), NOR the actual concrete retry-feedback message
    (naming the exact missed markers and saying explicitly "rewrite so
    every sentence ends with its bracket marker") got qwen2.5:3b to add
    one — all seven attempts reproduced the identical uncited prose.
    `_uncited_citation_matches` only ever returns a citation when a
    specific sentence already clears the overlap bar, so insertion here
    is fully deterministic — there's always a well-defined place to put
    the marker, and no LLM round-trip (and its retry-budget cost) is
    needed for a fix code can already make correctly.

    Multiple citations best-matching the SAME sentence get appended
    together in that sentence, e.g. '...these safeguards [1][2].';
    otherwise each marker goes immediately before ITS sentence's own
    trailing punctuation — the same position SYSTEM_PROMPT's own citation
    example uses ('X did Y [2].') — or at the sentence's end if it has no
    trailing .!? (the last sentence in an answer, sometimes). Matching by
    exact substring position (not by rebuilding from the split sentences)
    preserves the original text's exact whitespace/paragraph breaks
    outside the touched sentences.

    Returns `(possibly-modified content, citations actually inserted)` —
    `check_output` uses the second value to know a fix was applied (for
    its own metric) and recomputes `likely_uncited_citations` against the
    NEW content afterward rather than assuming this emptied it, though by
    construction it always does.
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


# Coarse sentence splitter — same "good enough, no NLP dependency" posture
# as `_content_words`'s own word regex. Splits after ./!/? followed by
# whitespace; a citation marker like "[3]" never contains those
# characters, so it always stays attached to the sentence it terminates.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Shared by _likely_uncited_citations and _likely_misattributed_citations
# below — a sentence shorter than this has too little vocabulary for
# either function's overlap ratio to mean anything, regardless of which
# direction that ratio is being checked (a real, distinctive-vocabulary
# source drawn on heavily, or a real, in-range marker attached to content
# its own source doesn't support). Judging by the SENTENCE's length, not
# the citation's, matters for both: a short (even single-word) source
# can't inflate either ratio by coincidence just because it's short — see
# _likely_misattributed_citations's own docstring for the live bug this
# was found from.
_MIN_JUDGED_SENTENCE_WORDS = 4
# Deliberately looser than _UNCITED_OVERLAP_RATIO's 0.6 (and in the
# OPPOSITE direction: a LOW ratio here still counts as "supported" — this
# only needs to catch a citing sentence that shares essentially NOTHING
# with its own marker's source, not police close paraphrasing the other
# way).
_MISATTRIBUTED_OVERLAP_RATIO = 0.25


def _likely_misattributed_citations(
    content: str, citations: list[dict], used: list[dict]
) -> list[dict]:
    """The mirror image of `_likely_uncited_citations`: instead of a real
    source used without a marker, this catches a real, in-range marker used
    on a sentence that shares no meaningful vocabulary with THAT marker's
    own source text — real bug, found live via Langfuse (trace ed435567):
    the model cited [3] on every sentence of an answer about database
    scalability, when [3]'s actual retrieved content had nothing to do with
    it. `_ungrounded_claims_count` doesn't catch this at all — [3] is a
    real, in-range marker, not an invented one — and `used_citations`
    doesn't either, since the marker genuinely does appear in the text.

    For each marker `used` in the answer, every sentence that cites it is
    checked against that marker's own source text. Only flags when NONE of
    a marker's citing sentences show meaningful overlap AND at least one
    was long enough to judge — a marker whose only citing sentences are all
    too short to score isn't flagged.

    Judges by `_MIN_JUDGED_SENTENCE_WORDS` on the SENTENCE, not the
    citation's own length — a short (even single-word) source can't
    inflate `overlap / len(sentence_words)` just because it's short, the
    way it could if the floor were on `cite_words` instead. Real bug,
    found live via Langfuse: a one-word memory ("magiclab396") cited three
    times on an answer about bundled skills for report-writing went
    undetected specifically because this function used to skip any
    citation short enough on ITS OWN length, missing the clearest, least
    ambiguous case of misattribution there is — a source with almost no
    vocabulary of its own to have been drawn on at all.
    `_likely_uncited_citations` was later brought in line with this same
    sentence-length floor for the identical reason, in the other
    direction (see its own docstring). Only skipped when a source has NO
    content words at all — nothing to compare against, not merely few.
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
