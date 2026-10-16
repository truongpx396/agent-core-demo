"""Message helpers with no dependency on `State` or node logic — a leaf module.

- Telling the person's own messages from the graph's retry artifacts
  (`RETRY_ARTIFACT_KEY`, `is_user_message`), and locating the last/previous user
  message (`last_human_message`, `previous_human_message`).
- Where the current turn starts (`current_turn_messages`).
- Answering a tool batch that will not run (`reject_tool_calls`).
- Building retrieval query text for a vague follow-up (`retrieval_query`, with the crude
  word-overlap helper `content_words` it and the citation checks share).
- Normalizing a `HumanMessage`'s content — plain string or multimodal content list
  (pattern 44) — to its text portion (`human_text`, `human_has_content`).

`human_text`/`human_has_content` are also unit-tested directly by
`tests/agent/graph/test_multimodal.py`.
"""
import re
from typing import TypeGuard

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

# Tags a message the graph wrote into the thread to drive a retry, so the
# readers below can tell it from conversation. `retry_output` appends its
# corrective feedback as a HumanMessage (the only role a model will treat as
# an instruction to act on), and tags the rejected answer it answers. Both
# stay in the thread through the retry loop, because the retry needs them;
# everything that looks for "the user's message" or "this turn" skips them,
# and the agent node drops earlier turns' ones before it calls the model.
# See GRAPH_PATTERNS.md pattern 5. Never set on an AIMessage with tool_calls:
# dropping that one later would orphan its ToolMessages.
RETRY_ARTIFACT_KEY = "retry_artifact"


# Tags a compaction breadcrumb SystemMessage so
# runtime/stream.py::get_session_messages can pick it out — every OTHER
# SystemMessage in a thread (only a legacy persisted prompt, see
# is_legacy_prompt) stays hidden from transcript replay as always. A
# SystemMessage specifically: _messages_to_trim already excludes all
# SystemMessages from both token count and removal candidates, so this
# breadcrumb is free and never itself gets trimmed later — unlike
# history_summary (replaced each compaction) or the context/summary
# SystemMessages agent() synthesizes per-call and never persists. The agent
# node passes it to the model; a legacy prompt it does not. See
# GRAPH_PATTERNS.md pattern 41.
COMPACTION_MARKER_KEY = "compaction_marker"


def is_compaction_marker(message: BaseMessage) -> bool:
    return bool((getattr(message, "additional_kwargs", None) or {}).get(COMPACTION_MARKER_KEY))


def is_legacy_prompt(message: BaseMessage) -> bool:
    """A SystemMessage persisted in a thread's history that is not a compaction breadcrumb.

    Today nothing writes one: the agent node prepends the domain's CURRENT system prompt on every
    call (`graph/agent_node.py`), so a prompt edit reaches every thread with no migration. Threads
    created before that hold the prompt (sometimes two, after a prompt edit) as their first
    message; those are ignored when the call is built, never shown to the model, and left in the
    checkpoint (they are skipped by trimming and by the transcript replay too)."""
    return isinstance(message, SystemMessage) and not is_compaction_marker(message)


def is_retry_artifact(message: BaseMessage) -> bool:
    return bool((getattr(message, "additional_kwargs", None) or {}).get(RETRY_ARTIFACT_KEY))


def is_user_message(message: BaseMessage) -> TypeGuard[HumanMessage]:
    """A HumanMessage the person sent, not retry feedback the graph wrote."""
    return isinstance(message, HumanMessage) and not is_retry_artifact(message)


# The corrective feedback `retry_output` wrote BEFORE it tagged anything (#168): untagged HumanMessages
# that read, in every thread that had a retry, as something the person said. Seven fixed sentences
# (their wording has not changed since they were introduced), each with the parts that identify it.
# Anything else is left alone: only a message that starts like one of these, has its fixed
# middle, and sits right after an answer the graph rejected is treated as feedback.
_LEGACY_RETRY_FEEDBACK = (
    ("That answer repeated internal system instructions.", ()),
    ("That answer was too short — please give a fuller answer.", ()),
    ("That answer showed a script and its output as if a real tool had run it", ()),
    ("The skill you loaded said to use ", (" for this — you computed a number by hand",)),
    ("You described using a tool instead of actually calling it", ()),
    ("That answer uses facts from source(s) ", (" without citing them. Do not call any tools",)),
    ("That answer cites ", (", but its content does not actually support what you wrote",)),
)


def _is_legacy_retry_feedback(text: str) -> bool:
    return any(
        text.startswith(start) and all(part in text for part in parts)
        for start, parts in _LEGACY_RETRY_FEEDBACK
    )


def legacy_retry_artifact_ids(messages: list[BaseMessage]) -> set[str]:
    """Ids of the retry artifacts an older build left in a thread untagged: the feedback, and the
    answer it rejected (the message right before it, with no tool calls).

    A heuristic by construction, since nothing marked them: the text must be one of the old
    feedback sentences AND directly follow an untagged answer. The cost of a false match is one
    message hidden from the model and the transcript, and it needs a person to type one of
    these sentences, in full, as their reply to an answer."""
    ids: set[str] = set()
    for i in range(1, len(messages)):
        feedback, answer = messages[i], messages[i - 1]
        if (
            isinstance(feedback, HumanMessage)
            and not is_retry_artifact(feedback)
            and feedback.id is not None
            and isinstance(feedback.content, str)
            and _is_legacy_retry_feedback(feedback.content)
            and isinstance(answer, AIMessage)
            and not answer.tool_calls
            and not is_retry_artifact(answer)
        ):
            ids.add(feedback.id)
            if answer.id is not None:
                ids.add(answer.id)
    return ids


def heal_legacy_retry_artifacts(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Tagged copies of the legacy retry artifacts in `messages`, to return from a node: they keep
    their ids, so `add_messages` replaces them in the thread rather than appending."""
    ids = legacy_retry_artifact_ids(messages)
    return [
        m.model_copy(update={"additional_kwargs": {**m.additional_kwargs, RETRY_ARTIFACT_KEY: True}})
        for m in messages
        if m.id in ids
    ]


def last_human_message(messages: list[BaseMessage]) -> HumanMessage | None:
    return next((m for m in reversed(messages) if is_user_message(m)), None)


def previous_human_message(messages: list[BaseMessage], before_index: int) -> HumanMessage | None:
    """The HumanMessage immediately before `before_index` — the prior
    turn's question, used by `retrieval_query` to enrich a vague
    follow-up. `before_index` is retrieve_context's `anchor`, so
    `messages[:before_index]` is everything before THIS turn. None on the
    first turn."""
    return next(
        (m for m in reversed(messages[:before_index]) if is_user_message(m)),
        None,
    )


# Below this many content words, a query rarely has enough distinctive
# vocabulary for hybrid search to match anything real. "pls be more the
# detailed" scores 3 ("be"/"the" are stopwords); real questions score well
# above it. 4 sits below genuine questions and at/above short follow-ups
# worth enriching.
_VAGUE_QUERY_MAX_CONTENT_WORDS = 4


def retrieval_query(current_text: str, previous_human: HumanMessage | None) -> str:
    """The text `retrieve_context` searches on — the current turn's
    question, unless too vague/short to search meaningfully alone, in
    which case the PRIOR turn's question is folded in too.

    Found live (Langfuse trace `e46c97c4`): "pls be more the detailed"
    alone matched nothing in Qdrant, so the model answered with generic
    filler while still reusing `[1]`/`[2]` citations from the previous
    turn — check_output flagged it as ungrounded but is directional-only
    and never retries over it, so the answer shipped as-is.

    Folding in the prior question gives the search real vocabulary again
    (same move a human makes re-reading the last question). Only one turn
    back, not the whole history — a rarer multi-vague-follow-up chain
    isn't chased, and reaching further risks a stale topic.
    """
    if previous_human is not None and len(content_words(current_text)) <= _VAGUE_QUERY_MAX_CONTENT_WORDS:
        return f"{human_text(previous_human)} {current_text}"
    return current_text


def human_text(message: BaseMessage | None) -> str:
    """The TEXT portion of a HumanMessage's content — a plain string, or
    a multimodal content list (`[{"type": "text", ...}, {"type":
    "image_url", ...}]`, pattern 44) built by
    `runtime/stream.py::_build_human_content` when an image is attached.
    Downstream text-only consumers (moderation, cache key, retrieval
    query) read through this. An image-only message yields "", not an
    error — see `human_has_content` for why that's not "no content."
    """
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def human_has_content(message: BaseMessage | None) -> bool:
    """True if this message has SOME real content — non-empty text OR at
    least one image part. A plain `human_text(message).strip()` check
    alone would wrongly reject a genuine image-only question as empty
    input in route_after_validation."""
    if message is None:
        return False
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if human_text(message).strip():
        return True
    return any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)


def current_turn_messages(messages: list) -> list:
    """Messages in the CURRENT turn only — from the most recent message the
    person sent (inclusive; `retry_output`'s feedback is a HumanMessage too but
    not a turn start, see `is_user_message`) onward, never spanning into a
    prior turn.
    Shared by graph/routing.py's `_current_turn_tool_call_batches`
    (pattern 34) and `skipped_required_sandbox_after_skill`, each
    filtering it differently afterward."""
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])),
        None,
    )
    return messages[last_human_index:] if last_human_index is not None else messages


def reject_tool_calls(tool_calls: list, reason: str) -> list[ToolMessage]:
    """Every pending tool_call needs a matching ToolMessage, or the next LLM
    call fails OpenAI's tool-response validation — shared by
    human_approval's rejection path and too_many_tool_calls below, which
    both need to abort a batch of tool calls without running them.

    Each rejection carries the tool's `name`, like a real result does:
    `pending_skill_required_tool` counts a required tool as dealt with only when a ToolMessage
    NAMES it, so a call the person rejected (or any batch aborted here) used to leave the tool
    "pending", and the agent kept being told to call what had just been declined."""
    return [ToolMessage(content=reason, tool_call_id=tc["id"], name=tc["name"]) for tc in tool_calls]


# A small, deliberately crude stopword list — good enough to stop common
# words from inflating an overlap ratio, without pulling in an NLP
# dependency for what's fundamentally a coarse, directional heuristic.
_STOPWORDS = frozenset(
    "a an the is are was were and or of to in for on at by with from that "
    "this it its be as you your can will if not into their his her they "
    "he she we".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def content_words(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2
    }
