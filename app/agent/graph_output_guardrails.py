"""The deferral/fabrication/prompt-leak heuristics behind `check_output`
(`app/agent/graph_routing.py`): narrated-instead-of-acting detection
(`_defers_instead_of_acting`), fabricated tool output
(`_fabricates_tool_output`), a fabricated reference-list footer
(`_strip_fabricated_reference_footer`), a skill-required tool skipped
(`_skipped_required_sandbox_after_skill`), a system-prompt leak
(`_leaks_system_prompt`), and the priority arbitration across all of
these plus the citation checks (`_retry_reason`). Split out of
`app/agent/graph_routing.py` purely for file size — see that module's own
docstring, and `app/agent/graph_loop_guards.py`/`app/agent/graph_citations.py`
for the sibling splits. No behavior change from the pre-split
single-file version.
"""
import re

from app.agent.graph import MIN_ANSWER_LENGTH
from app.agent.graph_skills import _pending_skill_required_tool
from app.agent.graph_tools import _current_turn_messages

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
# verbatim — same reasoning `_MIN_JUDGED_SENTENCE_WORDS` (graph_citations.py)
# applies to its word-count threshold, just on characters here since a leak
# is judged by verbatim reproduction, not topical word overlap.
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
