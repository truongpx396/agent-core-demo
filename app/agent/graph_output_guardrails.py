"""Deferral/fabrication/prompt-leak heuristics behind `check_output`
(`app/agent/graph_routing.py`): narrated-instead-of-acting
(`_defers_instead_of_acting`), fabricated tool output
(`_fabricates_tool_output`), a fabricated reference-list footer
(`_strip_fabricated_reference_footer`), a skipped skill-required tool
(`_skipped_required_sandbox_after_skill`), a system-prompt leak
(`_leaks_system_prompt`), and priority arbitration across all of these
plus the citation checks (`_retry_reason`). Split out of
`app/agent/graph_routing.py` for file size (see sibling splits
`graph_loop_guards.py`/`graph_citations.py`); no behavior change.
"""
import re

from app.agent.graph import MIN_ANSWER_LENGTH
from app.agent.graph_skills import _pending_skill_required_tool
from app.agent.graph_tools import _current_turn_messages

# Real bug, found live via Langfuse: qwen2.5:3b narrated tool intent
# ("I will use the `query_employees` tool...") or asked permission ("Would
# you like to know more?") instead of calling a tool, even after
# SYSTEM_PROMPT explicitly forbade it (a 3B model's instruction-following
# isn't reliable enough for a prompt-only fix). No real tool_calls exists
# in these cases, so check_output only sees an ordinary, useless final
# answer.
#
# Two phrasings, matched separately: (1) first-person tool intent that
# never became a real call ("I will/can/could/would use the X tool", "let
# me look that up"), and (2) asking permission to proceed instead of
# answering ("would you like me to proceed?") — also a separate
# SYSTEM_PROMPT violation.
#
# Widened twice more from later live failures: "I'll count the
# occurrences..." (a contraction + "count the" wasn't covered — the
# resulting incomplete answer got cached and replayed until expiry,
# Langfuse `9336aaa6`) and "Let's run this script in a sandbox..." (model
# narrating a real returned script instead of running it — "run this/
# that/it/the X" added). Both closed narrowly rather than generalized —
# same "deliberately crude" posture as the rest of this module.
_TOOL_INTENT_RE = re.compile(
    r"\b(?:i (?:will|can|could|would)|i'll|let(?:'s| us)|let me)\b"
    # Real false positive: "Sorry, I could not run that calculation."
    # matches "i could" + "run that" as readily as a genuine deferral.
    # This negative lookahead rejects a negation right after the lead-in
    # ("'t" / " not") before it reaches the trailing alternatives. Caught
    # by a hermetic test (tests/core/test_metrics.py) where the false
    # positive triggered an unwanted retry round, exhausted a fake LLM's
    # queued messages, and crashed the run with a raw StopIteration —
    # illustrating this heuristic being wrong can break a turn outright,
    # not just waste a retry.
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
    """True when the final answer narrates tool intent or asks permission
    instead of calling a tool or answering directly. Only meaningful on a
    message with no real tool_calls — check_output's only caller already
    guarantees that.

    Deliberately crude regex, same posture as this module's other checks —
    first-person only, so a third-person description of the agent's own
    capabilities doesn't trip it.
    """
    if not content or not isinstance(content, str):
        return False
    return bool(_TOOL_INTENT_RE.search(content) or _PERMISSION_SEEKING_RE.search(content))


# Two full ```...``` fenced blocks (open+close each) = 4 total ``` markers.
_FABRICATED_OUTPUT_FENCE_THRESHOLD = 4

# Strips fenced content before _CLAIMS_EXECUTION_RE runs, below — the
# narration this catches ("Running the calculation...", "Output:") has
# only ever appeared in the surrounding PROSE in every real case seen
# (introducing/following a fence), never inside the code itself, so this
# costs no true-positive detection while ruling out a code SAMPLE's own
# content (e.g. a literal `console.log` call) ever being misread as a
# claim of having actually run something.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# The actual "as if this really ran" signal a fabricated script+output
# pair is narrated with in every real case caught so far — see this
# function's own docstring for why the fence count ALONE isn't enough.
_CLAIMS_EXECUTION_RE = re.compile(
    r"\b(?:running|executing)\b|\b(?:output|result)s?\s*(?::|\s+(?:is|was|are|were)\b)",
    re.IGNORECASE,
)


def _fabricates_tool_output(content: str) -> bool:
    """True when a tool-call-free final answer contains 2+ markdown code
    fences AND narrates them as if actually executed (`_CLAIMS_EXECUTION_RE`)
    — the "here's the script, here's its output" shape presented as if
    run_command_in_sandbox actually ran, with no real tool_calls entry.

    Real bug, found live: after a sandbox approval was declined, the model
    invented both a script and its "output," narrated in PRESENT tense
    ("Running the calculation...") so `_defers_instead_of_acting` never
    caught it — and the fabricated math didn't even match the fabricated
    code (claimed 43750.00 for a calc that's actually 36450.00), yet
    passed every other check.

    The fence-count check ALONE, without the execution-claim requirement,
    is a real false positive found live: asked to showcase markdown
    formatting, the model correctly produced several purely illustrative
    fenced examples (no execution claimed anywhere) and got flagged as
    fabrication anyway, retried twice into the identical "problem," and
    fell back to a generic "I wasn't able to..." message for a wholly
    legitimate request (Langfuse trace `f8b1675b`, 2026-09-20). A single
    code block (e.g. explaining a formula) was already exempt via the
    fence threshold; this exempts a MULTI-block but purely illustrative
    answer the same way, without giving up the real detection — every
    true-positive case on record narrates execution in the surrounding
    prose (see `_CLAIMS_EXECUTION_RE`'s own comment)."""
    if not content or not isinstance(content, str):
        return False
    if content.count("```") < _FABRICATED_OUTPUT_FENCE_THRESHOLD:
        return False
    prose = _FENCE_RE.sub("", content)
    return bool(_CLAIMS_EXECUTION_RE.search(prose))


# Matches a markdown REFERENCE-DEFINITION line ("[1]: link/text") — never
# this app's citation convention (an inline "[n]" marker only, no colon,
# no reference list). Requires the colon so an inline marker at a line
# start isn't mistaken for one.
_REFERENCE_FOOTER_LINE_RE = re.compile(r"^\[\d+\]:\s")


def _strip_fabricated_reference_footer(content: str) -> str:
    """Strips a trailing markdown-style reference list the model sometimes
    appends after its own inline `[n]` markers, e.g.:

        ...meets the needs of your project. [1][2]

        [1]: [Link to the book or resource]

    Real bug, found live (Langfuse `e46c97c4`, 2026-09-09): qwen2.5:3b
    pattern-matched a different citation convention (academic reference
    lists) onto this app's inline-only one — the "link" is always
    fabricated, since this app never gives the model a URL to cite.

    Only strips an unbroken run of `_REFERENCE_FOOTER_LINE_RE` matches at
    the very end of `content` (plus a separating blank line); returns
    `content` unchanged if no such footer exists.
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
    """The tool name `_pending_skill_required_tool` still names, if the
    model's final answer (no more tool_calls) states a specific dollar
    figure without ever calling it — the model ignored a skill's own
    instruction not to estimate this kind of number by hand. Returns the
    actual tool name (not just a bool) so retry_output's feedback can
    name the specific tool a skill required.

    Real bug, found live (Langfuse `633eee2b`, 2026-09-08): a loaded
    deal-economics skill said to use a tool for this exact math; the
    model computed $141,862.50 via prose arithmetic instead. That number
    happened to be correct, but nothing enforced it, and other freehand
    attempts in the same session landed on wrong numbers — this closes
    the gap between "the skill said to use a tool" and it actually being
    used.

    Narrower than `_pending_skill_required_tool` itself: only fires when
    the final answer states a dollar figure — a clarifying question or an
    honest "I couldn't compute this" isn't the problem."""
    turn_messages = _current_turn_messages(messages)
    pending_tool = _pending_skill_required_tool(turn_messages)
    if not pending_tool:
        return None
    last = turn_messages[-1] if turn_messages else None
    content = getattr(last, "content", "") if last else ""
    if isinstance(content, str) and _DOLLAR_FIGURE_RE.search(content):
        return pending_tool
    return None


# Long enough that reusing a few words of instructions ("Be concise and
# direct") can't trip this, short enough to catch a real recitation
# without needing the whole prompt reproduced — same reasoning as
# graph_citations.py's _MIN_JUDGED_SENTENCE_WORDS, but on characters since
# a leak is judged by verbatim reproduction, not topical overlap.
_SYSTEM_PROMPT_LEAK_MIN_CHARS = 60
# Smaller than the window itself so a leak starting at any alignment is
# still caught by some checked window.
_SYSTEM_PROMPT_LEAK_STEP = 30


def _leaks_system_prompt(content: str, system_prompt: str) -> bool:
    """True when the final answer contains a long-enough VERBATIM run of
    the seeded system prompt to be a real leak, not coincidental overlap —
    output-side defense-in-depth complementing app/agent/moderation.py's
    input-side screening; an injection moderation's regexes miss can still
    be caught here if it actually gets the model to recite instructions
    back.

    Crude verbatim-substring check, not paraphrase-aware — same posture as
    moderation.py: catches direct recitation (the common jailbreak form),
    not a paraphrased/translated leak. Whitespace is normalized on both
    sides so reflowed text still matches.
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
    act on this round — same priority order as those two (leaked prompt,
    length, fabricated, skipped-tool, deferred, uncited, misattributed),
    centralized so check_output can compare this round's reason against
    the prior round's (retry_reason_repeat_count) without duplicating the
    ordering. Returns None when no retry is needed.

    Leaked prompt is checked first — it's the only reason with a real
    security dimension, and severe enough to trip the leak check that it
    can't also be merely too-short. Fabricated is checked ahead of
    deferred (presenting false info as true is worse than merely
    narrating intent); skipped-tool right after fabricated, still ahead
    of deferred (the model was explicitly told by a skill to use a tool
    and didn't, regardless of how the answer reads).
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
