"""Human-message helpers with no dependency on `State` or any node's own
logic: locating the last/previous `HumanMessage` in a list
(`_last_human_message`, `_previous_human_message`), building the actual
retrieval query text for a possibly-vague follow-up (`_retrieval_query`),
and normalizing a `HumanMessage`'s content — plain string or a multimodal
content list (GRAPH_PATTERNS.md pattern 44) — down to its text portion
(`_human_text`, `_human_has_content`). Split out of `app/agent/graph.py`
purely for file size — see that module's own docstring, and
`app/agent/graph_compaction.py`/`app/agent/graph_agent_node.py`/
`app/agent/graph_retrieval.py`/`app/agent/graph_cache.py` for the sibling
splits that use these. No behavior change from the pre-split single-file
version.

`_human_text`/`_human_has_content` are also directly unit-tested by
`tests/agent/test_multimodal.py`, imported from here now rather than from
`app.agent.graph`.
"""
from langchain_core.messages import BaseMessage, HumanMessage


def _last_human_message(messages: list[BaseMessage]) -> HumanMessage | None:
    return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)


def _previous_human_message(messages: list[BaseMessage], before_index: int) -> HumanMessage | None:
    """The HumanMessage immediately before position `before_index` in
    `messages` — the prior turn's own question, used by `_retrieval_query`
    to enrich a vague follow-up's search. `before_index` is
    `retrieve_context`'s own `anchor` (the current turn's question is,
    at that point, the last message in state), so `messages[:before_index]`
    is exactly everything said before THIS turn. None on the first turn
    of a conversation."""
    return next(
        (m for m in reversed(messages[:before_index]) if isinstance(m, HumanMessage)),
        None,
    )


# Below this many content words, a query rarely carries enough distinctive
# vocabulary for hybrid search to match anything real — see
# _retrieval_query's own docstring for the live case that surfaced this.
# "pls be more the detailed" scores 3 ("pls", "more", "detailed" — "be"
# and "the" are stopwords); an ordinary, self-contained question like
# "how do I build a production-ready AI agent?" scores well above it
# (build, production, ready, agent, ...). 4 sits below real questions and
# at/above the shortest genuine follow-ups worth enriching anyway.
_VAGUE_QUERY_MAX_CONTENT_WORDS = 4


def _retrieval_query(current_text: str, previous_human: HumanMessage | None) -> str:
    """The text `retrieve_context` actually searches on — the current
    turn's own question, UNLESS it's too vague/short to search on
    meaningfully by itself, in which case the PRIOR turn's own question is
    folded in too.

    Real bug, found live via Langfuse (trace `e46c97c4`, 2026-09-09): a
    follow-up of "pls be more the detailed" alone matched nothing in
    Qdrant (verified: `retrieve_context`'s own output that turn was
    `citations: []`, `context length: 0`), so the model answered with
    generic filler while still habitually reusing `[1]`/`[2]` from the
    PREVIOUS turn's real citations — check_output correctly flagged both
    as ungrounded (`ungrounded_claims_count=2`), but that check is
    directional-only by design (see its own docstring) and was never
    going to retry over it, so the ungrounded answer shipped as-is.

    Folding in the prior turn's own question gives the SAME search real
    vocabulary to work with — the same move a human makes re-reading the
    last question before answering "can you elaborate?" — and, live-
    verified (see this function's own test), finds the SAME real content
    again for the exact query that originally returned nothing. Only one
    turn back, not the whole history: a chain of several vague follow-ups
    in a row is a real but much rarer case this doesn't chase, and
    reaching further back risks pulling in a topic several turns stale.
    """
    # Deferred: app/agent/graph_utils.py imports `app.agent.graph` back at
    # its own top level, and `app.agent.graph` re-exports names from THIS
    # module at ITS own top level (see graph.py's own docstring) — so
    # importing graph_utils back here at THIS module's own top level would
    # close a real cycle.
    from app.agent.graph_utils import _content_words

    if previous_human is not None and len(_content_words(current_text)) <= _VAGUE_QUERY_MAX_CONTENT_WORDS:
        return f"{_human_text(previous_human)} {current_text}"
    return current_text


def _human_text(message: BaseMessage | None) -> str:
    """The TEXT portion of a HumanMessage's content, whether it's a plain
    string (the overwhelmingly common, text-only case) or a multimodal
    content list — `[{"type": "text", ...}, {"type": "image_url", ...}]`,
    the shape app/agent/runtime_stream.py::_build_human_content builds when an image is
    attached (GRAPH_PATTERNS.md pattern 44). Everywhere downstream logic
    only cares about the WORDS, not the raw content the model actually
    receives, reads through this: moderation screening, the semantic
    cache key, the retrieval query. An image-only message (no text part
    at all) yields "", not an error — see `_human_has_content` below for
    why that must NOT be treated as "no content."
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


def _human_has_content(message: BaseMessage | None) -> bool:
    """True if this message has SOME real content worth acting on —
    non-empty text OR at least one image part. A plain
    `_human_text(message).strip()` check alone would wrongly reject a
    genuine image-only question ("what's in this picture?", no text at
    all) as empty input in route_after_validation."""
    if message is None:
        return False
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if _human_text(message).strip():
        return True
    return any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)
