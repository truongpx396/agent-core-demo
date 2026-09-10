"""The two conditional-edge gate functions — `should_continue` (after
`agent`: tool call, final answer, or safety budget?) and `check_output`
(is the final answer good enough to end the turn on?) — plus the private
helpers used *exclusively* by each. Split out of `app/agent/graph.py`
purely for file size; `State`, every node function/factory, and the other
routing functions (`route_after_validation`, `route_after_moderation`,
`route_after_cache`, `route_after_compaction`, `route_after_approval`)
stay there since they're tied to nodes that stay there too. See
`app/agent/graph.py`'s own module docstring, and `app/agent/graph_build.py`/
`app/agent/graph_build_subagent.py` for the two graph-assembly functions
that wire both gates below into a compiled graph. No behavior change from
the pre-split single-file version.

A handful of helpers `should_continue`/`check_output` depend on
deliberately did NOT move here, even though they look like they belong —
each is also called directly by a node function that stays in
`app/agent/graph.py` (`_invalid_tool_call_names`/`_DEFAULT_VALID_TOOL_NAMES`
by the `invalid_tool_call` node, `app/agent/graph_tools.py`;
`_current_turn_messages` by `make_agent_node`, also `graph_tools.py`;
`_pending_skill_required_tool` by `make_agent_node`,
`app/agent/graph_skills.py`; `_content_words` by `_retrieval_query`,
`app/agent/graph_utils.py`), so moving them INTO `graph.py` would force it
to import back from here — a real circular import, avoided by each living
in its own sibling file instead, imported from there below (never from
`graph.py` directly for these five).
"""
import json
import re
from collections.abc import Mapping
from typing import Literal, cast

from langchain_core.messages import AIMessage
from langgraph.prebuilt import tools_condition

from app.agent.graph import (
    MAX_CONSECUTIVE_SAME_RETRY_REASON,
    MAX_ITERATIONS,
    MAX_REPEATED_ACTIONS,
    MAX_TOKENS_PER_TURN,
    MAX_TOOL_CALLS_PER_TURN,
    MIN_ANSWER_LENGTH,
    SYSTEM_PROMPT,
    State,
)
from app.agent.graph_skills import _pending_skill_required_tool
from app.agent.graph_tools import (
    _DEFAULT_VALID_TOOL_NAMES,
    _current_turn_messages,
    _invalid_tool_call_names,
)
from app.agent.graph_utils import _content_words
from app.agent.tools import TOOL_CAPABILITIES
from app.core import metrics
from app.core.config import MAX_COST_USD_PER_TURN


def _tool_capability(name: str, tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES) -> str:
    """A tool absent from `tool_capabilities` defaults to "outward" — fail
    closed, so a new tool added to a domain's TOOLS without a capability
    entry is gated rather than silently trusted. This is the one place
    that default is applied; everywhere else just reads the mapping.
    `tool_capabilities` defaults to app/agent/tools.py's TOOL_CAPABILITIES (the
    Ecorp domain) so every existing direct call/import keeps working
    unchanged; `build_graph` passes a domain's own mapping instead (see
    its docstring and app/agent/manifest.py, GRAPH_PATTERNS.md pattern 23)."""
    return tool_capabilities.get(name, "outward")


def _tool_call_fingerprint(tool_calls: list) -> str:
    """A stable fingerprint for one batch of tool calls — same tool
    name(s) + same args, independent of call order, so a model repeating
    the identical action (not just a coincidentally similar one) is what
    gets detected. Sorted so a batch of [A, B] and [B, A] fingerprint
    identically."""
    normalized = sorted(
        (tc["name"], json.dumps(tc["args"], sort_keys=True)) for tc in tool_calls
    )
    return json.dumps(normalized)


def _current_turn_tool_call_batches(messages: list) -> list:
    """Tool-call batches from AIMessages within the CURRENT turn only, in
    reverse order (most recent first) — never spanning into a prior
    turn's tool calls. This is a per-turn loop-progress check
    (GRAPH_PATTERNS.md pattern 34), not a cross-conversation one: a model
    that called search_docs last turn and calls it again this turn hasn't
    repeated anything."""
    turn_messages = _current_turn_messages(messages)
    return [
        m.tool_calls
        for m in reversed(turn_messages)
        if isinstance(m, AIMessage) and m.tool_calls
    ]


def _consecutive_repeat_count(messages: list) -> int:
    """How many of the most recent consecutive tool-call batches (within
    this turn) share the current one's fingerprint — a pure function of
    `state["messages"]`, no extra State field needed to track it, since
    the message history already IS the record of what's been tried."""
    batches = _current_turn_tool_call_batches(messages)
    if not batches:
        return 0
    target = _tool_call_fingerprint(batches[0])
    count = 0
    for batch in batches:
        if _tool_call_fingerprint(batch) != target:
            break
        count += 1
    return count


def _mandatory_gate_reason(
    tool_calls: list, tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES
) -> str | None:
    """None if every call in this batch is read_only; otherwise the more
    severe capability present ("outward" — including any undeclared tool —
    over "mutating"), used only to label the metric in should_continue.
    Gating itself doesn't care which one — either forces human_approval."""
    capabilities = {_tool_capability(tc["name"], tool_capabilities) for tc in tool_calls}
    if "outward" in capabilities:
        return "outward"
    if "mutating" in capabilities:
        return "mutating"
    return None


def _use_skill_called_without_search(tool_calls: list, messages: list) -> bool:
    """True if this batch calls `use_skill` but `skill_search` was never
    called earlier in THIS turn — SYSTEM_PROMPT requires skill_search
    first specifically so the model looks up a skill's real name instead
    of guessing one. `use_skill`'s own "no skill named X found" response
    (app/agent/tools.py) already recovers gracefully from a WRONG name,
    but nothing stopped the model from inventing one outright and never
    searching at all. Real bug, found live via Langfuse (trace
    `197ab4e1`, 2026-09-09): the model called
    `use_skill(name="build_production_ai_agents")` — a name with no basis
    in the actual catalog whatsoever (verified: no skill in this app's
    bundled catalog remotely resembles it) — for an ordinary "how do I
    build X" question that had nothing to do with any packaged skill and
    already had highly relevant retrieved context to answer from
    directly. It then narrated the resulting "no skill found" failure
    straight into the user-facing answer once use_skill returned it —
    an internal tool-naming miss leaking out as if it were part of the
    real answer.

    Checked over `_current_turn_tool_call_batches` (already-established
    per-turn helper, GRAPH_PATTERNS.md pattern 34) — a skill_search from
    an EARLIER turn doesn't license skipping it on a fresh question now.
    """
    if not any(tc["name"] == "use_skill" for tc in tool_calls):
        return False
    return not any(
        any(tc["name"] == "skill_search" for tc in batch)
        for batch in _current_turn_tool_call_batches(messages)
    )


# --- Edge fn: after agent, route to tools / output check / abort ---
def should_continue(
    state: State,
    tool_capabilities: Mapping[str, str] = TOOL_CAPABILITIES,
    valid_tool_names: frozenset[str] = _DEFAULT_VALID_TOOL_NAMES,
    max_iterations: int = MAX_ITERATIONS,
    max_tokens: int = MAX_TOKENS_PER_TURN,
    max_cost_usd: float = MAX_COST_USD_PER_TURN,
) -> Literal[
    "tools",
    "human_approval",
    "too_many_tool_calls",
    "invalid_tool_call",
    "use_skill_without_search",
    "check_output",
    "no_answer",
]:
    """Did the LLM call a tool, give a final answer, or hit a safety budget?

    Checked in order: the iteration cap and token cap are turn-wide safety
    nets (checked first, regardless of what the LLM just said); then
    whether this is a tool call at all; then whether it's *too many* tool
    calls at once; then whether any of them isn't a real registered tool at
    all (`invalid_tool_call` — see `_invalid_tool_call_names`); then whether
    `use_skill` was called without `skill_search` earlier this turn
    (`use_skill_without_search` — see `_use_skill_called_without_search`);
    then whether it needs human approval before running. Both the
    invalid-name and use_skill-without-search checks run BEFORE the
    human-approval gate deliberately: neither is a real tool call anyone
    could meaningfully approve or reject (a malformed name outright, or a
    real tool called on a guessed argument the model was explicitly told
    to look up first), so both are rejected and looped back to `agent` for
    a self-correcting retry instead of ever reaching a human with garbage
    to review — `use_skill` is read_only anyway (never reaches
    human_approval on its own merits), but the ordering still matters for
    consistency with the invalid-name check right above it.

    Two independent reasons route to `human_approval`, and only one of them
    is optional:
    - `require_approval` on the input state — opt-in (default False), so
      the existing CLI/API behavior is unchanged unless a caller asks for
      it. See app/channels/chat.py's `--hitl` mode.
    - Any pending tool_call whose declared capability (`tool_capabilities`
      — app/agent/tools.py::TOOL_CAPABILITIES by default, or a domain's own
      mapping, see below) isn't "read_only" — mandatory, never skippable
      via `require_approval=False`. A retrieval-augmented agent already
      carries untrusted content on essentially every turn (GRAPH_PATTERNS.md
      pattern 12); once that's true, letting a mutating or outward-reaching
      tool run unsupervised too is exactly the "two of three legs" exposure
      this app has no reason to gamble on (see app/agent/tools.py::TOOL_CAPABILITIES
      for the full reasoning). An *undeclared* tool is treated the same as
      "outward," so forgetting to register a new tool's capability fails
      toward extra caution, not past it.

    `tool_capabilities`/`valid_tool_names` both default to the Ecorp domain's
    mapping/tool set so every existing test/caller invoking
    `should_continue(state)` directly is unaffected; `build_graph` binds a
    domain's own values for both via `functools.partial` before registering
    this as the `agent` node's conditional edge — see its docstring and
    GRAPH_PATTERNS.md pattern 23. `max_iterations`/`max_tokens`/`max_cost_usd`
    default to the same module constants the bare-global checks used before
    this signature grew these params — added so a NESTED subagent run
    (GRAPH_PATTERNS.md pattern 46) can be bound to its own, smaller
    MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN ceiling via the identical `functools.partial`
    mechanism, independent of the parent turn's own remaining budget.
    This stays a plain module-level function (not a factory, unlike
    `agent`/`retrieve_context`/the semantic-cache nodes) specifically so it
    remains directly importable and callable with just `state`, matching
    every other routing function in this file (see this module's own
    docstring on why routing functions live at module level).

    All four safety-net exits below route to `"no_answer"`, not straight to
    `END` — `no_answer_fallback` is the one place that turns "some budget
    fired before `check_output` ever ran" into a real, user-visible message
    instead of silently ending the turn on whatever the `agent` node's last
    AIMessage happened to be (often empty — a small model that fails to
    produce a tool call or any content still burns real tokens doing it, so
    a `retry_output` loop can hit `max_tokens` before ever producing prose).
    Same "empty final AIMessage means a safety net tripped" signal
    `run_subagent` (app/agent/tools.py) already uses for a NESTED run;
    this is the top-level-turn equivalent, which previously had none.
    """
    if state.get("iterations", 0) >= max_iterations:
        return "no_answer"
    # Folds any run_subagent spend THIS turn into the parent's own live
    # ceiling (GRAPH_PATTERNS.md pattern 46's disclosed gap) — each nested
    # run is still separately, independently bounded by its own
    # MAX_SUBAGENT_TOKENS_PER_RUN/MAX_SUBAGENT_COST_USD_PER_RUN; this only
    # makes the PARENT aware that delegating doesn't happen for free.
    subagent_spend = state.get("subagent_spend", [])
    effective_tokens = state.get("total_tokens", 0) + sum(t for t, _ in subagent_spend)
    if effective_tokens >= max_tokens:
        metrics.agent_token_budget_exceeded_total.inc()
        return "no_answer"
    effective_cost_usd = state.get("total_cost_usd", 0.0) + sum(c for _, c in subagent_spend)
    if effective_cost_usd >= max_cost_usd:
        # A HARD stop (GRAPH_PATTERNS.md pattern 35) — independent of the
        # token cap above: the same token count costs differently on
        # different model tiers, so a $ ceiling is not a derived quantity
        # of the token one, it's its own budget.
        metrics.agent_cost_ceiling_exceeded_total.inc()
        return "no_answer"
    result = tools_condition(state)  # type: ignore[arg-type]  # State is a valid Mapping at runtime; tools_condition's stub just doesn't say so
    if result != "tools":
        return "check_output"
    tool_calls = cast(AIMessage, state["messages"][-1]).tool_calls or []
    if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
        return "too_many_tool_calls"
    if _invalid_tool_call_names(tool_calls, valid_tool_names):
        return "invalid_tool_call"
    if _use_skill_called_without_search(tool_calls, state["messages"]):
        return "use_skill_without_search"
    if _consecutive_repeat_count(state["messages"]) >= MAX_REPEATED_ACTIONS:
        # Checked here, independently of MAX_ITERATIONS — a run looping
        # on one identical action would otherwise just exhaust the
        # iteration cap and settle spend indistinguishably from a run
        # that was actually converging (GRAPH_PATTERNS.md pattern 34).
        metrics.agent_no_progress_total.inc()
        return "no_answer"
    mandatory_reason = _mandatory_gate_reason(tool_calls, tool_capabilities)
    if mandatory_reason:
        metrics.agent_capability_gate_total.labels(capability=mandatory_reason).inc()
    if state.get("require_approval") or mandatory_reason:
        return "human_approval"
    return "tools"


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
# as _WORD_RE above. Splits after ./!/? followed by whitespace; a citation
# marker like "[3]" never contains those characters, so it always stays
# attached to the sentence it terminates.
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


# Real bug, found live via Langfuse across several turns on the same
# thread: instead of actually calling query_employees, qwen2.5:3b kept
# writing prose ANNOUNCING that it would ("I will use the `query_employees`
# tool to look up..."), or outright asking permission first ("I can look
# that up for you. Would you like to know more?") — SYSTEM_PROMPT already
# said explicitly not to do this (added the same day this was caught), but
# a 3B model's instruction-following isn't reliable enough for a prompt-only
# fix to close this the way it closed the citation-omission case (verified:
# the SAME "I will use the tool... would you like me to proceed?" pattern
# reproduced AFTER that prompt change shipped). Every "yes" the user sent in
# reply just restarted the identical cycle, since there was never a real
# tool_calls list to route on `should_continue`'s tools_condition branch —
# check_output only ever sees these as ordinary (if useless) final answers.
#
# Two independent phrasings, matched separately since neither observed
# instance had both: (1) first-person intent to use/look something up
# ("I will/can/could/would use the X tool", "let me look that up") that
# never turned into a real tool call this round, and (2) asking the user's
# permission to proceed instead of just answering ("would you like me to
# proceed?", "shall I go ahead?") — the second also already violates
# SYSTEM_PROMPT's separate "don't ask if the user wants to know more" rule,
# so flagging it is doubly justified regardless of tool intent specifically.
#
# A THIRD real instance, found live, widened phrasing (1) rather than
# adding a third category: after a run_command_in_sandbox script failed,
# the model announced "Let's try parsing the log manually instead. I'll
# count the occurrences of 'db_timeout'..." and just STOPPED there — never
# produced the actual count. This slipped through the original pattern for
# two independent reasons: "I'll" (a contraction) wasn't recognized as
# equivalent to "I will", and neither "count the X"/"parse X manually" was
# in the trailing-phrase list (which only covered tool-specific verbs like
# "look up"/"check"/"search for"). Both gaps closed narrowly — "i'll" added
# as its own lead-in alternative, "count the"/"calculate"/"compute"/
# "manually" added as trailing alternatives — rather than attempting a more
# general "does this message actually deliver what it promises" check,
# same "deliberately crude, not exhaustive" posture as every other
# heuristic in this module. Consequence of the miss, not just a missed
# retry: check_output accepted the incomplete answer as final, and it was
# then written to the semantic cache (app/retrieval/semantic_cache.py) —
# every future semantically-similar question would have kept replaying
# this same non-answer until the cache entry expired, not just this one
# turn (Langfuse trace `9336aaa6`, 2026-09-08).
#
# A FOURTH real instance, found live immediately after fixing
# skill_tools_first's own bug (app/agent/tools.py) — once the model
# actually started calling use_skill (see that function's docstring), the
# NEXT failure point was narrating the skill's own returned script instead
# of running it: "Here's the script to compute the total contract
# value:\n\n```python\n...\n```\n\nLet's run this script in a sandbox to
# get the result." — a complete, correct script, quoted verbatim, followed
# by an announcement to run it that never became a real tool_calls entry.
# "run this"/"run that"/"run it"/"run the X" added as trailing
# alternatives for the same reason count/calculate/compute were: the
# original list only covered "look up"/"check"/"search for", never the
# single most common verb for what this app's own sandbox tools actually
# do.
_TOOL_INTENT_RE = re.compile(
    r"\b(?:i (?:will|can|could|would)|i'll|let(?:'s| us)|let me)\b"
    # A real, live-verified false positive, found the same session this was
    # widened: "Sorry, I could not run that calculation." matches "i could"
    # (lead-in) + "run that" (trailing) just as readily as a genuine
    # deferral — an apology for FAILURE, not a promise to act, but the
    # regex couldn't tell the difference. This negative lookahead rejects
    # a negation immediately after the lead-in ("'t" for can't/couldn't/
    # wouldn't, " not" for the separate-word form) before the match can
    # even reach the trailing-phrase alternatives. Caught by a hermetic
    # test (tests/core/test_metrics.py's own tool-error path) that starts
    # a fake LLM with exactly two queued messages: the false positive
    # triggered an unwanted retry_output round, the fake model's message
    # iterator ran out on the THIRD call it was never told to expect, and
    # that raw StopIteration surfaced as LangGraph's own pregel loop
    # crashing with "generator raised StopIteration" — a good illustration
    # of why this heuristic being wrong isn't just a wasted retry, it can
    # break the turn outright.
    r"(?!'t\b|\s+not\b)"
    r"[^.!?\n]{0,60}"
    r"\b(?:use\s+the\s+\S+\s+tool|look\s+(?:that|this|it)\s+up|"
    r"look\s+up\s+(?:that|this|it)|check\s+(?:on\s+)?that|"
    r"search\s+for\s+that|proceed\s+with\s+that|"
    r"count\s+the|calculate\s+(?:that|this|it)|compute\s+(?:that|this|it)|"
    r"run\s+(?:this|that|it|the\s+\S+)|"
    r"manually)\b",
    re.IGNORECASE,
)
_PERMISSION_SEEKING_RE = re.compile(
    r"\b(?:would you like|do you want|shall i|want me to|should i)\b[^.!?\n]{0,40}\?",
    re.IGNORECASE,
)


def _defers_instead_of_acting(content: str) -> bool:
    """True when the final answer narrates an intent to use a tool, or asks
    the user's permission to proceed, instead of just calling the tool or
    answering directly. Only ever meaningful on a message with NO real
    tool_calls (check_output's only caller already guarantees that — a
    genuine tool call routes through should_continue's `tools_condition`
    branch and never reaches here at all), so no need to check that here.

    Deliberately crude regex matching, same posture as every other
    heuristic in this module — first-person phrasing only (`_TOOL_INTENT_RE`
    requires "I will/can/could/would", not "this agent can"), so a
    legitimate THIRD-PERSON description of the agent's own capabilities
    (e.g. answering "what tools do you have?") doesn't trip it.
    """
    if not content or not isinstance(content, str):
        return False
    return bool(_TOOL_INTENT_RE.search(content) or _PERMISSION_SEEKING_RE.search(content))


# Two full ```...``` fenced blocks (open+close each) = 4 total ``` markers.
_FABRICATED_OUTPUT_FENCE_THRESHOLD = 4


def _fabricates_tool_output(content: str) -> bool:
    """True when a tool-call-free final answer contains two or more
    markdown code fences — the specific shape of "here's the script"
    immediately followed by "here's its output," presented as if
    run_command_in_sandbox had actually run, when no tool_calls entry
    exists for this message at all (check_output's only caller already
    guarantees that — same precondition _defers_instead_of_acting already
    documents). A single code block (explaining a formula, or showing what
    a script would look like) is normal and not flagged; two or more is
    the shape a genuine script-plus-its-output pair takes.

    Real bug, found live: after a run_command_in_sandbox approval was
    declined once, the model invented BOTH a plausible-looking Python
    script AND a plausible-looking "output" line for it, narrated in
    PRESENT tense ("Running the calculation script with the provided
    inputs:") — not narrated future intent, so _defers_instead_of_acting's
    own phrasing never caught it. The fabricated arithmetic didn't even
    match the fabricated code (`50000 * (1-0.10)**3` is 36450.00, not the
    claimed 43750.00) — this reached the user as an ordinary, confident
    final answer, undetected by every other check (content wasn't too
    short, didn't leak the prompt, had no citations to misattribute)."""
    if not content or not isinstance(content, str):
        return False
    return content.count("```") >= _FABRICATED_OUTPUT_FENCE_THRESHOLD


# Matches a markdown REFERENCE-DEFINITION line ("[1]: some link/text") —
# never this app's own citation convention, which is exclusively an
# inline "[n]" marker with no colon and no separate reference list
# anywhere. Requires the colon specifically so a real inline marker at
# the start of a line ("[3] some sentence continuing a paragraph.") is
# never mistaken for one.
_REFERENCE_FOOTER_LINE_RE = re.compile(r"^\[\d+\]:\s")


def _strip_fabricated_reference_footer(content: str) -> str:
    """Strips a trailing markdown-style reference list the model
    sometimes appends after its own inline `[n]` markers, e.g.:

        ...meets the needs of your project and users. [1][2]

        [1]: [Link to the book or resource]
        [2]: [Link to the book or resource]

    Real bug, found live via Langfuse (trace `e46c97c4`, 2026-09-09):
    qwen2.5:3b pattern-matched a DIFFERENT citation convention it saw in
    training data (academic/web citations with a reference list at the
    bottom) onto this app's inline-only one — the "link" is always
    fabricated (this app never gives the model a URL to cite; retrieved
    content is numbered passages, not sources with links), so the footer
    can only ever mislead a reader into thinking a real reference exists.

    Only strips lines matching `_REFERENCE_FOOTER_LINE_RE` found in an
    unbroken run at the very END of the content (plus one blank line
    separating it from the real answer) — never touches a legitimate
    inline `[n]` marker anywhere earlier in the prose, and returns
    `content` completely unchanged (not even whitespace-trimmed) when no
    such footer is present at all.
    """
    if not isinstance(content, str) or not content:
        return content
    lines = content.rstrip().splitlines()
    end = len(lines)
    while end > 0 and _REFERENCE_FOOTER_LINE_RE.match(lines[end - 1]):
        end -= 1
    if end == len(lines):
        return content
    return "\n".join(lines[:end]).rstrip()


_DOLLAR_FIGURE_RE = re.compile(r"\$[\d,]+(?:\.\d{1,2})?")


def _skipped_required_sandbox_after_skill(messages: list) -> str | None:
    """The tool name _pending_skill_required_tool still names, if the
    model produced a FINAL answer this round (no more tool_calls) that
    states a specific dollar figure without ever calling it — the model
    read "write a short script and run it with run_command_in_sandbox
    instead... don't estimate this kind of number in your head" and
    estimated it in its head anyway, ignoring even agent()'s own
    proactive reminder. Returns the actual name (not just a bool) so
    retry_output's own feedback can name the SPECIFIC tool a skill
    required, not a hardcoded one — same reasoning as
    _pending_skill_required_tool's own docstring.

    Real bug, found live (Langfuse trace `633eee2b`, 2026-09-08): the
    deal-economics skill was loaded, its own text says exactly the above,
    and the final answer computed a $141,862.50 figure via step-by-step
    PROSE arithmetic instead of ever calling the tool. That particular
    number happened to be correct (independently verified against the
    skill's own formula) — but nothing here actually enforced that, and
    every OTHER live attempt at this same freehand deal math earlier in
    this session landed on a materially wrong number instead. Getting
    lucky once is not the same as being reliable; this closes the gap
    between "the skill said to use a tool" and "the tool was actually
    used," rather than trusting whatever number the model happens to
    produce by hand.

    Deliberately narrow in one more way beyond _pending_skill_required_tool
    itself: only fires when the final answer states a dollar figure (a
    clarifying question, or an honest "I couldn't compute this," is not
    the problem this exists to catch)."""
    turn_messages = _current_turn_messages(messages)
    pending_tool = _pending_skill_required_tool(turn_messages)
    if not pending_tool:
        return None
    last = turn_messages[-1] if turn_messages else None
    content = getattr(last, "content", "") if last else ""
    if isinstance(content, str) and _DOLLAR_FIGURE_RE.search(content):
        return pending_tool
    return None


# Long enough that a coincidental short-phrase overlap (the model
# naturally reusing a few words of its own instructions, e.g. "Be concise
# and direct") can't trip this, short enough to catch a real "repeat your
# instructions" recitation without needing the WHOLE prompt reproduced
# verbatim — same reasoning `_MIN_JUDGED_SENTENCE_WORDS` above applies to
# its word-count threshold, just on characters here since a leak is
# judged by verbatim reproduction, not topical word overlap.
_SYSTEM_PROMPT_LEAK_MIN_CHARS = 60
# Step between checked windows — smaller than the window itself so a leak
# starting at any alignment still gets caught (a leak that starts exactly
# mid-window, with non-overlapping windows, could otherwise fall between
# two checked chunks and go undetected).
_SYSTEM_PROMPT_LEAK_STEP = 30


def _leaks_system_prompt(content: str, system_prompt: str) -> bool:
    """True when the final answer contains a long-enough VERBATIM run of
    the seeded system prompt's own text to be a real leak, not
    coincidental phrasing overlap — output-side defense-in-depth
    complementing app/agent/moderation.py's input-side screening (see that
    module's docstring on why it's pattern-based, not exhaustive): an
    injection phrased in a way moderation's known-pattern regexes don't
    catch can still be caught HERE if it actually succeeds in getting the
    model to recite its instructions back — the two checks watch different
    ends of the same turn, not the same thing twice.

    Deliberately a crude verbatim-substring check, not a paraphrase-aware
    one — same "known patterns, not exhaustive" posture as
    app/agent/moderation.py: catches a direct recitation (the
    overwhelmingly common form a successful "repeat your instructions"
    jailbreak takes), not a paraphrased or translated leak. Whitespace is
    normalized on both sides first (collapsing newlines/multiple spaces to
    one) so reflowed text still matches.
    """
    if not content or not system_prompt or not isinstance(content, str):
        return False
    normalized_content = " ".join(content.split()).lower()
    normalized_prompt = " ".join(system_prompt.split()).lower()
    window = _SYSTEM_PROMPT_LEAK_MIN_CHARS
    if len(normalized_prompt) < window:
        return False
    for i in range(0, len(normalized_prompt) - window + 1, _SYSTEM_PROMPT_LEAK_STEP):
        if normalized_prompt[i : i + window] in normalized_content:
            return True
    return False


def _retry_reason(
    content: str,
    leaks_prompt: bool,
    fabricated: bool,
    skipped_tool: str | None,
    deferred: bool,
    likely_uncited: list[dict],
    likely_misattributed: list[dict],
) -> str | None:
    """Which single reason (if any) route_after_check/retry_output would
    act on for this round — same priority order those two already use
    (leaked system prompt, then length, then fabricated-tool-output, then
    skipped-required-tool, then deferred-instead-of-acting, then uncited,
    then misattributed), pulled into one place so check_output can compare
    THIS round's reason against the PRIOR round's (see
    retry_reason_repeat_count) without duplicating that ordering a third
    time. Returns None when the answer needs no retry at all.

    Leaked system prompt is checked FIRST, ahead of even length: it's the
    one reason here with a real security dimension (see
    _leaks_system_prompt's own docstring), and a leak severe enough to
    trip a 60-char verbatim-run check is never ALSO going to be too short
    to matter — the two conditions can't meaningfully co-occur, so
    ordering them relative to each other is really about which gets named
    in the feedback on the rare turn where both were somehow true.

    Fabricated tool output is checked ahead of deferred-instead-of-acting
    (both are "no real tool call happened" problems, but presenting FALSE
    information as true is worse than merely narrating an intention to act
    — see _fabricates_tool_output's own docstring for the live case that
    motivated this ordering). Skipped-required-tool is checked right after
    fabricated, still ahead of deferred — it doesn't invent a fake tool
    result the way fabricated does, but it's still a specific, more severe
    problem than generic deferral: the model was TOLD (by a skill it
    itself just loaded) to use a tool for this exact kind of math, and
    used none, no matter how the answer happens to read (see
    _skipped_required_sandbox_after_skill's own docstring).
    """
    if leaks_prompt:
        return "leaked_prompt"
    if isinstance(content, str) and len(content) < MIN_ANSWER_LENGTH:
        return "too_short"
    if fabricated:
        return "fabricated"
    if skipped_tool:
        return "skipped_tool"
    if deferred:
        return "deferred"
    if likely_uncited:
        return "uncited"
    if likely_misattributed:
        return "misattributed"
    return None


# --- Node: check output — also extracts which offered citations the
# final answer actually used (see _used_citations) and how many cited
# markers were invented (see _ungrounded_claims_count). Recomputed from
# scratch every time this node runs, so a retry_output loop back to
# `agent` (a new answer, possibly citing different sources) doesn't leave
# stale values from the rejected short answer.
#
# `system_prompt` defaults to the module-level SYSTEM_PROMPT (the Ecorp
# domain's) so `graph.check_output(state)` stays directly callable exactly
# as every existing test already calls it — same "plain module-level
# function, not a factory" shape should_continue's own
# tool_capabilities/valid_tool_names defaults already use, for the
# identical reason (see should_continue's docstring). build_graph binds
# the CORRECT per-domain prompt via functools.partial, same mechanism as
# domain_should_continue. ---
def check_output(state: State, system_prompt: str = SYSTEM_PROMPT) -> dict:
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    citations = state.get("citations") or []

    # Strip a fabricated reference-list footer FIRST, before any
    # citation-related computation below — it isn't a real answer sentence
    # to judge grounding on either way, and cleaning it up front means
    # every field this node returns already reflects the text the user
    # will actually see.
    message_update: dict = {}
    cleaned_content = _strip_fabricated_reference_footer(content)
    if cleaned_content != content:
        metrics.agent_reference_footer_stripped_total.inc()
        content = cleaned_content
        message_update["messages"] = [last.model_copy(update={"content": content})]

    used = _used_citations(content, citations)
    if citations and not used and content:
        # Directional signal only (the opposite failure mode from
        # ungrounded_claims_count below) — not enforced. A legitimate
        # general-knowledge or calculator-only answer looks IDENTICAL to a
        # model that silently dropped a mandatory citation: retrieve_context
        # always returns its top-K docs regardless of actual relevance, and
        # the SYSTEM_PROMPT explicitly allows citing nothing for either of
        # those cases. route_after_check deliberately doesn't retry on
        # this, same reasoning as why it doesn't retry on a high
        # ungrounded_claims_count either — see this metric's own docstring.
        metrics.agent_zero_citations_total.inc()
    likely_misattributed = _likely_misattributed_citations(content, citations, used)
    if likely_misattributed:
        metrics.agent_misattributed_citations_total.inc()
    defers = _defers_instead_of_acting(content)
    if defers:
        metrics.agent_deferred_instead_of_acting_total.inc()
    fabricated = _fabricates_tool_output(content)
    if fabricated:
        metrics.agent_fabricated_tool_output_total.inc()
    skipped_tool = _skipped_required_sandbox_after_skill(state["messages"])
    if skipped_tool:
        metrics.agent_skipped_required_tool_total.inc()
    leaks_prompt = _leaks_system_prompt(content, system_prompt)
    if leaks_prompt:
        metrics.agent_system_prompt_leak_total.inc()

    # Auto-correct rather than retry: _insert_missing_citation_markers's
    # own docstring has the live evidence (7 different prompt-level
    # attempts, all failed) for why this fixes the marker directly instead
    # of routing to retry_output over it. `used`/`likely_uncited`/`content`
    # are all recomputed against the CORRECTED text below so every other
    # field this node returns (and _retry_reason's own inputs) reflect
    # what the user will actually see, not the pre-correction draft. Reuses
    # `message_update` from the reference-footer strip above, if that
    # already fired this round — both corrections compose onto the SAME
    # final message rather than each overwriting the other's fix.
    likely_uncited = _likely_uncited_citations(content, citations, used)
    if likely_uncited:
        corrected_content, fixed = _insert_missing_citation_markers(content, citations, used)
        if fixed:
            metrics.agent_citation_auto_inserted_total.inc()
            content = corrected_content
            used = _used_citations(content, citations)
            likely_uncited = _likely_uncited_citations(content, citations, used)
            message_update["messages"] = [last.model_copy(update={"content": content})]

    reason = _retry_reason(
        content, leaks_prompt, fabricated, skipped_tool, defers, likely_uncited, likely_misattributed
    )
    prior_reason = state.get("last_retry_reason")
    if reason is None:
        repeat_count = 0
    elif reason == prior_reason:
        repeat_count = (state.get("retry_reason_repeat_count") or 0) + 1
    else:
        repeat_count = 1
    return {
        **message_update,
        "used_citations": used,
        "ungrounded_claims_count": _ungrounded_claims_count(content, citations),
        "likely_uncited_citations": likely_uncited,
        "likely_misattributed_citations": likely_misattributed,
        "deferred_instead_of_acting": defers,
        "fabricated_tool_output": fabricated,
        "skipped_required_tool": skipped_tool,
        "leaks_system_prompt": leaks_prompt,
        "last_retry_reason": reason,
        "retry_reason_repeat_count": repeat_count,
    }


def route_after_check(
    state: State,
) -> Literal["retry_output", "retry_exhausted", "suggest_followups"]:
    reason = state.get("last_retry_reason")
    if reason is None:
        return "suggest_followups"
    if (state.get("retry_reason_repeat_count") or 0) >= MAX_CONSECUTIVE_SAME_RETRY_REASON:
        # The SAME rejection reason fired on consecutive rounds — the model
        # isn't converging (see MAX_CONSECUTIVE_SAME_RETRY_REASON's own
        # docstring), so another retry_output round would just spend a
        # real LLM call for the same outcome we can already predict.
        metrics.agent_retry_exhausted_total.inc()
        return "retry_exhausted"
    return "retry_output"
