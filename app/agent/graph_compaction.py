"""Token estimation and history trimming/compaction: `_estimate_tokens`
(a tiktoken-based approximate budget check), `_messages_to_trim`/
`_trim_history` (which whole turns fall outside the kept window),
`_format_turns_for_summary`/`make_compact_history_node` (folding whatever
gets trimmed into a running `history_summary` instead of just discarding
it, AR-015a), and `COMPACTION_MARKER_KEY`/`_compaction_marker_message`
(the permanent breadcrumb left in `state["messages"]` when a compaction
actually did something). Split out of `app/agent/graph.py` purely for
file size — see that module's own docstring, and
`app/agent/graph_messages.py` for the sibling split (the human-message
helpers this file's `_format_turns_for_summary` uses). No behavior change
from the pre-split single-file version.

Nothing in `app/agent/graph.py` itself needs anything from this file —
`make_compact_history_node` is wired into the compiled graph by
`app/agent/graph_build.py` directly, and `COMPACTION_MARKER_KEY` is read
by `app/agent/runtime_stream.py::get_session_messages` — so, unlike
`app/agent/graph_messages.py`, this file is a pure one-directional
dependency on `graph.py` (for `HISTORY_TOKEN_CEILING`/`HISTORY_TOKEN_FLOOR`),
never imported back from it.
"""
import logging
from typing import cast

import tiktoken
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from app.agent.graph import HISTORY_TOKEN_CEILING, HISTORY_TOKEN_FLOOR, State
from app.agent.graph_messages import _human_text
from app.core import metrics

logger = logging.getLogger(__name__)

# Not Qwen's own tokenizer (no local equivalent bundled with this app) — a
# deliberately approximate, directional budget check, the same "good enough,
# not byte-exact" posture MIN_ANSWER_LENGTH's char-count already takes
# elsewhere in this app. tiktoken is already an installed dependency (pulled
# in transitively by langchain-openai), so this adds nothing new. Built once,
# lazily, at module scope — cheap to reuse, not cheap to rebuild per call.
_TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "") or ""
    return content if isinstance(content, str) else str(content)


def _estimate_tokens(messages: list[BaseMessage]) -> int:
    return sum(len(_TOKEN_ENCODING.encode(_message_text(m))) for m in messages)


def _messages_to_trim(
    messages: list[BaseMessage],
    ceiling: int = HISTORY_TOKEN_CEILING,
    floor: int = HISTORY_TOKEN_FLOOR,
) -> list[BaseMessage]:
    """The actual message OBJECTS (with content) that fall outside the kept
    window. Shared by `compact_history` (which needs the real content to
    summarize) and, via `_trim_history`, anything that only needs the ids to
    delete.

    Hysteresis, not a sliding window: does nothing while the estimated token
    count of the non-system history is at/under `ceiling`; once it's
    exceeded, drops whole OLDEST turns until the kept tail is at/under
    `floor` — a strictly lower bar than `ceiling` — so the very next turn
    starts from a real gap below the trigger point instead of sitting right
    back at it. See HISTORY_TOKEN_CEILING/FLOOR's own comments (app/agent/graph.py)
    for why a plain "always trim back to the same ceiling" design (this
    function's prior turn-count form) re-triggers on every single subsequent
    turn forever instead of occasionally. `ceiling`/`floor` are parameters
    (both defaulting to the module constants) purely for direct unit testing
    with small, controlled values — no production caller overrides them.

    Trims by whole turn (a HumanMessage through the next HumanMessage),
    never by raw message count, so a tool_call/ToolMessage pair is never
    split — an orphaned tool_call fails the next LLM call's validation
    exactly like the HITL-rejection gotcha `_reject_tool_calls` exists to
    avoid on the *current* turn, just triggered by trimming instead of a
    disapproval. The seeded system prompt (app/agent/runtime.py::_ensure_seeded_async)
    is never dropped, and the most recent turn is never dropped either — a
    turn so large on its own that even keeping just it exceeds `floor` still
    keeps it whole rather than cutting into it. A message with no id (only
    possible outside a compiled graph, e.g. a hand-built dict in a test) is
    left alone rather than guessed at, since RemoveMessage deletes by id.
    """
    turn_starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(turn_starts) <= 1:
        return []  # nothing to drop without losing the current turn itself
    non_system = [m for m in messages if not isinstance(m, SystemMessage)]
    if _estimate_tokens(non_system) <= ceiling:
        return []
    cutoff = turn_starts[-1]  # worst case: keep only the most recent turn
    for idx in turn_starts[1:]:
        kept = [m for m in messages[idx:] if not isinstance(m, SystemMessage)]
        if _estimate_tokens(kept) <= floor:
            cutoff = idx
            break
    return [
        m for m in messages[:cutoff] if not isinstance(m, SystemMessage) and m.id is not None
    ]


def _trim_history(
    messages: list[BaseMessage],
    ceiling: int = HISTORY_TOKEN_CEILING,
    floor: int = HISTORY_TOKEN_FLOOR,
) -> list[RemoveMessage]:
    """`_messages_to_trim` reduced to `RemoveMessage` deletion stubs —
    kept as its own function for callers (and tests) that only care which
    ids get removed, not their content."""
    return [RemoveMessage(id=cast(str, m.id)) for m in _messages_to_trim(messages, ceiling, floor)]


def _format_turns_for_summary(messages: list[BaseMessage]) -> str:
    """A plain-text rendering of the turns compact_history is about to
    discard — for the summarization prompt, not for anything downstream
    of the graph. Messages with no content (e.g. an AIMessage that's pure
    tool_calls) are skipped rather than rendered as an empty line. A
    multimodal HumanMessage (GRAPH_PATTERNS.md pattern 44) renders its
    text part only, via `_human_text` — the summarization LLM call here
    is text-only regardless of what the ORIGINAL turn attached, same
    scope boundary app/agent/runtime.py's module docstring draws for this
    feature: an attached image is seen once, by the model that answered
    the turn it was attached to, never re-sent on every later turn."""
    role_names = {"human": "User", "ai": "Assistant", "tool": "Tool"}
    lines = []
    for m in messages:
        content = _human_text(m) if isinstance(m, HumanMessage) else (getattr(m, "content", "") or "")
        if content:
            lines.append(f"{role_names.get(m.type, m.type)}: {content}")
    return "\n".join(lines)


_HISTORY_SUMMARY_PROMPT = (
    "Summarize the following earlier conversation turns concisely, in a "
    "few sentences. Preserve any facts, decisions, names, or commitments "
    "that might matter for later turns; drop small talk and anything "
    "already resolved.\n\n{prior_clause}"
    "Turns to summarize:\n{turns_text}\n\nSummary:"
)


# Tags a compaction breadcrumb SystemMessage (see _compaction_marker_message)
# so app/agent/runtime_stream.py::get_session_messages can pick it out specifically —
# every OTHER SystemMessage in state["messages"] (the seeded base
# SYSTEM_PROMPT) stays hidden from that transcript replay, same as always.
# A SystemMessage, deliberately: _messages_to_trim already excludes every
# SystemMessage from both its token count AND its removal candidates, so
# this breadcrumb costs nothing against HISTORY_TOKEN_CEILING and, once
# written, is never itself a target of a LATER compaction pass — it's
# meant to sit in state["messages"] permanently, unlike history_summary
# (a plain string field, folded/replaced on every compaction) or the
# context/summary SystemMessages agent() synthesizes fresh per call and
# never persists at all. See GRAPH_PATTERNS.md pattern 41 and the
# conversation this was added from: history_summary already survives
# across turns as STATE, but nothing in the persisted message list itself
# previously showed a human (or an admin replaying a session transcript)
# that older turns had been cut — this is that visible breadcrumb.
COMPACTION_MARKER_KEY = "compaction_marker"


def _compaction_marker_message(turns_dropped: int, *, summarized: bool) -> SystemMessage:
    turn_word = "turn" if turns_dropped == 1 else "turns"
    verb = "summarized" if summarized else "dropped (summarization unavailable)"
    return SystemMessage(
        content=f"[{turns_dropped} earlier {turn_word} {verb} to keep this conversation within budget.]",
        additional_kwargs={COMPACTION_MARKER_KEY: True},
    )


def make_compact_history_node(
    llm, ceiling: int = HISTORY_TOKEN_CEILING, floor: int = HISTORY_TOKEN_FLOOR
):
    """Factory, same rationale as make_agent_node/make_suggest_followups_node:
    needs an LLM client to turn discarded turns into a running summary
    instead of just discarding them (AR-015a).

    Runs once per turn, right after validate_input — the one point every
    turn passes through exactly once (see validate_input's comment) — but
    kept as its own node rather than folded into validate_input because it
    needs an LLM call and validate_input is meant to stay a plain,
    dependency-free function of state/config.

    `ceiling`/`floor` default to the module constants; overridable purely so
    tests can trigger/observe the hysteresis behavior with small, controlled
    token budgets instead of needing thousands of tokens of placeholder
    content — no production caller passes anything but the defaults.
    """

    async def compact_history(state: State) -> dict:
        """Whatever `_messages_to_trim` would discard gets folded into the
        running `history_summary` instead of just dropped — the trim
        itself (which ids get RemoveMessage'd) is unchanged from before;
        only what happens to their CONTENT is new.

        Degrades to trimming without updating the summary on any LLM
        failure — bounding `state["messages"]` must not depend on the
        summarization call succeeding, same reliability posture as
        suggest_followups.

        Every non-empty outcome also appends one `_compaction_marker_message`
        — a permanent, never-again-touched breadcrumb in `state["messages"]`
        itself (see COMPACTION_MARKER_KEY's own comment for why a
        SystemMessage is what makes "permanent" safe here), so a session's
        transcript replay (app/agent/runtime_stream.py::get_session_messages) can
        show that older turns were cut, not just silently show fewer turns
        than actually happened.

        `async def`/`ainvoke` (not `.invoke()`), same reasoning as `agent`
        (app/agent/graph_agent_node.py): a real LLM call is I/O, not CPU
        work, so it belongs on the event loop, not tying up a thread in
        the shared default executor every OTHER concurrent turn's nodes
        also queue behind.
        """
        to_summarize = _messages_to_trim(state["messages"], ceiling, floor)
        if not to_summarize:
            return {}

        removals = [RemoveMessage(id=cast(str, m.id)) for m in to_summarize]
        metrics.agent_history_compacted_total.inc()
        turns_dropped = sum(1 for m in to_summarize if isinstance(m, HumanMessage))

        prior_summary = state.get("history_summary") or ""
        try:
            prior_clause = (
                f"Existing summary so far (extend it, don't discard it):\n{prior_summary}\n\n"
                if prior_summary
                else ""
            )
            prompt = _HISTORY_SUMMARY_PROMPT.format(
                prior_clause=prior_clause,
                turns_text=_format_turns_for_summary(to_summarize),
            )
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            new_summary = (response.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - never fail the turn over a summary
            logger.warning(
                "history summarization failed; trimming without updating the summary",
                extra={"node": "compact_history", "error_class": type(exc).__name__},
            )
            marker = _compaction_marker_message(turns_dropped, summarized=False)
            return {"messages": [*removals, marker]}

        if not new_summary:
            marker = _compaction_marker_message(turns_dropped, summarized=False)
            return {"messages": [*removals, marker]}
        marker = _compaction_marker_message(turns_dropped, summarized=True)
        return {"messages": [*removals, marker], "history_summary": new_summary}

    return compact_history
