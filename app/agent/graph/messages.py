"""Human-message helpers with no dependency on `State` or node logic:
telling the person's own messages from the graph's retry artifacts
(`RETRY_ARTIFACT_KEY`, `is_user_message`), locating the last/previous
user message (`last_human_message`, `previous_human_message`), building
retrieval query text for a vague follow-up (`retrieval_query`), and
normalizing a `HumanMessage`'s content — plain string or multimodal content
list (pattern 44) — to its text portion (`human_text`,
`human_has_content`). Split out of `app/agent/graph/core.py` for file size.

`human_text`/`human_has_content` are also unit-tested directly by
`tests/agent/graph/test_multimodal.py`.
"""
from typing import TypeGuard

from langchain_core.messages import BaseMessage, HumanMessage

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


def is_retry_artifact(message: BaseMessage) -> bool:
    return bool((getattr(message, "additional_kwargs", None) or {}).get(RETRY_ARTIFACT_KEY))


def is_user_message(message: BaseMessage) -> TypeGuard[HumanMessage]:
    """A HumanMessage the person sent, not retry feedback the graph wrote."""
    return isinstance(message, HumanMessage) and not is_retry_artifact(message)


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
    # Deferred: graph/utils.py imports app.agent.graph.core at its own top
    # level, and app/agent/graph/core.py re-exports names from THIS module at its top
    # level — importing graph_utils back here at module level would close
    # a real cycle.
    from app.agent.graph.utils import content_words

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
