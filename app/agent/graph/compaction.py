"""Token estimation and history trimming/compaction: `_estimate_tokens` (tiktoken-based approximate
budget check), `_messages_to_trim`/`_trim_history` (which whole turns fall outside the kept window),
`_format_turns_for_summary`/`make_compact_history_node` (folding trimmed turns into a running
`history_summary` instead of discarding them, AR-015a), and `COMPACTION_MARKER_KEY`/
`_compaction_marker_message` (the permanent breadcrumb left in `state["messages"]` when compaction
does something).

Depends only on `graph/state.py` (for `HISTORY_TOKEN_CEILING`/`HISTORY_TOKEN_FLOOR`) and
`graph/messages.py`; `make_compact_history_node` is wired in by `graph/build.py`, and
`COMPACTION_MARKER_KEY` is read by `runtime/stream.py::get_session_messages` and
`graph/agent_node.py`.
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
from langchain_core.runnables import RunnableConfig

from app.agent.cost import metering
from app.agent.graph.messages import (
    COMPACTION_MARKER_KEY,
    human_text,
    is_retry_artifact,
    is_user_message,
)
from app.agent.graph.state import HISTORY_TOKEN_CEILING, HISTORY_TOKEN_FLOOR, State
from app.agent.graph.utils import without_tools
from app.core import gateway, metrics
from app.core.config import CHAT_MODEL

logger = logging.getLogger(__name__)

# No local Qwen tokenizer, so this is a deliberately approximate,
# directional check (same posture as MIN_ANSWER_LENGTH's char count).
# tiktoken is already pulled in transitively by langchain-openai. Built
# once at module scope since it's not cheap to rebuild per call.
_TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "") or ""
    return content if isinstance(content, str) else str(content)


def _estimate_tokens(messages: list[BaseMessage]) -> int:
    """What the model will be sent. A retry artifact (a rejected answer and the feedback it drew) stays in the
    stored history but `agent` drops an earlier turn's before every call, so it is not counted: counting it
    made a history that fits look over budget, and paid for a summary and a trim earlier than needed."""
    return sum(len(_TOKEN_ENCODING.encode(_message_text(m))) for m in messages if not is_retry_artifact(m))


def _messages_to_trim(
    messages: list[BaseMessage],
    ceiling: int = HISTORY_TOKEN_CEILING,
    floor: int = HISTORY_TOKEN_FLOOR,
) -> list[BaseMessage]:
    """The actual message OBJECTS (with content) that fall outside the kept
    window. Shared by `compact_history` (needs real content to summarize)
    and, via `_trim_history`, anything that only needs ids to delete.

    Hysteresis, not a sliding window: no-ops while non-system history is
    at/under `ceiling`; once exceeded, drops whole oldest turns until the
    kept tail is at/under `floor` (a strictly lower bar), so the next turn
    starts with real headroom instead of re-triggering every turn. See
    HISTORY_TOKEN_CEILING/FLOOR's comments (app/agent/graph/core.py) for why a plain
    "always trim to the same ceiling" design re-triggers forever.

    Trims by whole turn (HumanMessage through the next HumanMessage),
    never raw message count, so a tool_call/ToolMessage pair is never
    split (an orphaned tool_call fails LLM validation — same gotcha
    `reject_tool_calls` avoids for the current turn). The seeded system
    prompt and the most recent turn are never dropped. A message with no
    id is left alone rather than guessed at, since RemoveMessage deletes
    by id.
    """
    turn_starts = [i for i, m in enumerate(messages) if is_user_message(m)]
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
    discard, for the summarization prompt only. Messages with no content
    (e.g. pure tool_calls) are skipped. A multimodal HumanMessage
    (GRAPH_PATTERNS.md pattern 44) renders its text part only via
    `human_text` — an attached image is seen once, by the model that
    answered that turn, never re-sent later."""
    role_names = {"human": "User", "ai": "Assistant", "tool": "Tool"}
    lines = []
    for m in messages:
        if is_retry_artifact(m):
            continue  # a rejected answer and the feedback it drew, not part of the conversation
        content = human_text(m) if isinstance(m, HumanMessage) else (getattr(m, "content", "") or "")
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


def _compaction_marker_message(turns_dropped: int, *, summarized: bool) -> SystemMessage:
    turn_word = "turn" if turns_dropped == 1 else "turns"
    verb = "summarized" if summarized else "dropped (summarization unavailable)"
    return SystemMessage(
        content=f"[{turns_dropped} earlier {turn_word} {verb} to keep this conversation within budget.]",
        additional_kwargs={COMPACTION_MARKER_KEY: True},
    )


def make_compact_history_node(
    llm,
    ceiling: int = HISTORY_TOKEN_CEILING,
    floor: int = HISTORY_TOKEN_FLOOR,
    model_alias: str = CHAT_MODEL,
):
    """Factory, same rationale as make_agent_node/make_suggest_followups_node:
    needs an LLM client to turn discarded turns into a running summary
    (AR-015a) instead of just discarding them.

    Runs once per turn that reaches the model: after validate_input,
    moderation and a semantic-cache miss, right before retrieve_context
    (a screened-out or cache-hit turn pays for no summary). Kept as its own
    node since it needs an LLM call and validate_input stays a plain,
    dependency-free function.

    `ceiling`/`floor` default to the module constants; overridable only so
    tests can trigger the hysteresis behavior with small budgets.
    `model_alias` is the LiteLLM alias `llm` is bound to, used only to price and record its
    calls, the same meaning it has for `make_agent_node`.

    Sends no tool schemas (`utils.without_tools`): a transcript in, a summary out.
    """
    llm = without_tools(llm)

    async def compact_history(state: State, config: RunnableConfig | None = None) -> dict:
        """Whatever `_messages_to_trim` would discard gets folded into the
        running `history_summary` instead of dropped — the trim itself is
        unchanged; only what happens to the discarded CONTENT is new.

        Degrades to trimming without updating the summary on any LLM
        failure — bounding `state["messages"]` must not depend on
        summarization succeeding (same posture as suggest_followups).

        Every non-empty outcome also appends a `_compaction_marker_message`
        breadcrumb (see COMPACTION_MARKER_KEY) so a transcript replay can
        show that older turns were cut.

        `async def`/`ainvoke`, same reasoning as `agent`
        (graph/agent_node.py): a real LLM call is I/O and belongs on the
        event loop, not the shared default executor other turns' nodes
        queue behind.
        """
        to_summarize = _messages_to_trim(state["messages"], ceiling, floor)
        if not to_summarize:
            return {}

        removals = [RemoveMessage(id=cast(str, m.id)) for m in to_summarize]
        metrics.agent_history_compacted_total.inc()
        turns_dropped = sum(1 for m in to_summarize if is_user_message(m))

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
            # Metered like every other model call (app/agent/cost/metering.py): one usage event, and
            # NOT added to the turn's token/cost budget: a long history summary must not be able to
            # stop the answer the turn exists to give.
            call = await metering.metered_invoke(
                llm,
                [HumanMessage(content=prompt)],
                config=config,
                kind="compaction",
                model_alias=model_alias,
            )
            response = call.response
            new_summary = (response.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - never fail the turn over a summary
            gateway.note_budget_stop(exc)  # swallowed here, but the backstop firing is still counted
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
