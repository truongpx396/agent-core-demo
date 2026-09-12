"""Tests for the `agent` node in isolation, using a fake chat model instead
of the real ChatOpenAI client — see `make_agent_node`'s docstring in
app/agent/graph.py for why it's a factory.

`agent` is `async def` (calls `llm.ainvoke`, not `.invoke` — see its own
docstring), so every call below runs through `asyncio.run(...)` — this
repo's established pattern for exercising async code from a plain
`def test_...` (no pytest-asyncio configured; see app/agent/graph_utils.py's
`_instrumented` docstring for why only the I/O-bound nodes are async at
all)."""
import asyncio

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph import make_agent_node


class _RecordingFakeLLM:
    """Wraps GenericFakeChatModel and remembers every message list it was
    invoked with, so tests can assert on exactly what would be sent to the
    real model. A plain wrapper rather than a subclass because
    GenericFakeChatModel is a Pydantic model and rejects arbitrary instance
    attributes on subclasses.

    Defines `ainvoke` explicitly (not inherited — this wraps
    GenericFakeChatModel rather than subclassing it) since `agent` now
    calls `llm.ainvoke(...)`, never `.invoke()`."""

    def __init__(self, messages):
        self._inner = GenericFakeChatModel(messages=messages)
        self.seen_messages: list = []

    def invoke(self, messages, *args, **kwargs):
        self.seen_messages = list(messages)
        return self._inner.invoke(messages, *args, **kwargs)

    async def ainvoke(self, messages, *args, **kwargs):
        self.seen_messages = list(messages)
        return await self._inner.ainvoke(messages, *args, **kwargs)


def test_agent_invokes_llm_and_bumps_iterations():
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="42")]))
    agent = make_agent_node(fake_llm)

    state = {"messages": [HumanMessage(content="what is 21*2?")], "iterations": 3}
    result = asyncio.run(agent(state))

    assert result["iterations"] == 4
    assert result["messages"][0].content == "42"


def test_agent_defaults_missing_iterations_to_zero_then_one():
    fake_llm = GenericFakeChatModel(messages=iter([AIMessage(content="hi")]))
    agent = make_agent_node(fake_llm)

    result = asyncio.run(agent({"messages": [HumanMessage(content="hi")]}))
    assert result["iterations"] == 1


def test_agent_injects_context_as_system_message_when_present():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what is a checkpointer?")],
        "context": "doc: checkpointers persist graph state.",
    }
    asyncio.run(agent(state))

    assert any(
        isinstance(m, SystemMessage) and "checkpointers persist" in m.content
        for m in fake_llm.seen_messages
    )


def test_agent_skips_context_message_when_context_empty():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    asyncio.run(agent({"messages": [HumanMessage(content="hi")], "context": ""}))

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)


def test_agent_inserts_context_before_the_question_at_the_anchor():
    """context_anchor_index (set once by retrieve_context) says WHERE to
    splice context in — right before the turn's own question, not appended
    after whatever's currently last."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    state = {
        "messages": [question],
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    asyncio.run(agent(state))

    assert [type(m).__name__ for m in fake_llm.seen_messages] == [
        "SystemMessage",
        "HumanMessage",
        "SystemMessage",  # the tail-appended citation reminder — see
        # test_agent_appends_a_citation_reminder_after_the_question below
    ]
    assert fake_llm.seen_messages[1] is question


def test_agent_keeps_context_anchored_across_a_turns_own_tool_loop():
    """The whole point of the fix: on a LATER call within the same turn
    (more messages have since piled up after the anchor — a tool round, or
    a retry_output nudge), context must still land at the SAME anchor
    index, immediately before the original question, not at the new
    (shifted) tail — otherwise every call in a multi-round turn sends a
    DIFFERENT prefix and nothing about it stays cacheable."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    later_messages = [
        AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {}, "id": "1"}]),
        HumanMessage(content="That answer was too short — please give a fuller answer."),
    ]
    state = {
        "messages": [question, *later_messages],
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,  # still the original question's index
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert isinstance(seen[0], SystemMessage)
    assert "checkpointers persist" in seen[0].content
    assert seen[1] is question
    assert seen[2:4] == later_messages
    # The tail-appended citation reminder trails the accumulated tool/retry
    # messages too — recency-weighted, same as history_summary's reminder.
    assert isinstance(seen[4], SystemMessage)
    assert "bracket marker" in seen[4].content


def test_agent_falls_back_to_appending_context_without_an_anchor():
    """A hand-built State missing context_anchor_index (never ran through
    retrieve_context) must not crash — falls back to the old tail-append."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    state = {"messages": [question], "context": "doc: checkpointers persist graph state."}
    asyncio.run(agent(state))

    assert fake_llm.seen_messages[0] is question
    assert isinstance(fake_llm.seen_messages[1], SystemMessage)


def test_agent_injects_history_summary_as_system_message_when_present():
    """AR-015a (GRAPH_PATTERNS.md pattern 41) — compact_history's running
    summary reaches the model the same way retrieved context does."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what did we discuss earlier?")],
        "history_summary": "Earlier, the user asked about refund policy.",
    }
    asyncio.run(agent(state))

    assert any(
        isinstance(m, SystemMessage) and "refund policy" in m.content
        for m in fake_llm.seen_messages
    )


def test_history_summary_injection_tells_the_model_not_to_restate_it_verbatim():
    """Guardrail against a small model regurgitating its injected summary
    back into the final answer instead of treating it as background-only
    reference (observed against a real qwen2.5:3b deployment: a later
    question's answer dumped an earlier turn's summary/facts/citations all
    into one blob). The anti-regurgitation instruction lives in its OWN
    short, separate reminder message, positioned AFTER (closer to
    generation than) the bulk summary text — not baked into the summary
    message itself — so the summary's bulky content can sit in the stable,
    cache-friendly anchored prefix while only this one short line stays
    recency-weighted. See make_agent_node."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what did we discuss earlier?")],
        "history_summary": "Earlier, the user asked about refund policy.",
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    summary_idx = next(
        i for i, m in enumerate(seen) if isinstance(m, SystemMessage) and "refund policy" in m.content
    )
    reminder_idx = next(
        i for i, m in enumerate(seen) if isinstance(m, SystemMessage) and "do not restate" in m.content
    )
    assert "do not restate" not in seen[summary_idx].content, (
        "the instruction should live in its own message, not the summary text"
    )
    assert reminder_idx > summary_idx, "the reminder must stay closer to generation than the summary"


def test_agent_anchors_history_summary_before_the_question_same_as_context():
    """The bulk summary TEXT is front-loaded at the anchor (before the
    question), same as context — only the short anti-regurgitation
    reminder stays tail-appended. Summary comes before context: more
    general background first, the currently-relevant retrieved docs
    closest to the question."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what did we discuss earlier?")
    state = {
        "messages": [question],
        "history_summary": "Earlier, the user asked about refund policy.",
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert "refund policy" in seen[0].content
    assert "checkpointers persist" in seen[1].content
    assert seen[2] is question
    assert "do not restate" in seen[3].content


def test_agent_keeps_history_summary_anchored_across_a_turns_own_tool_loop():
    """Same guarantee as context's own anchor test: on a later call in the
    same turn, the summary TEXT must still land before the original
    question rather than after newly accumulated tool/retry messages —
    only the short reminder trails behind those."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what did we discuss earlier?")
    later_messages = [
        AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {}, "id": "1"}]),
        HumanMessage(content="That answer was too short — please give a fuller answer."),
    ]
    state = {
        "messages": [question, *later_messages],
        "history_summary": "Earlier, the user asked about refund policy.",
        "context_anchor_index": 0,
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert "refund policy" in seen[0].content
    assert seen[1] is question
    assert seen[2:4] == later_messages
    assert "do not restate" in seen[4].content


def test_agent_skips_history_summary_message_when_absent():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    asyncio.run(agent({"messages": [HumanMessage(content="hi")]}))

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)


def test_agent_appends_a_citation_reminder_after_the_question():
    """Real bug, found live via Langfuse: qwen2.5:3b drops the (already
    "mandatory") citation instruction in prompts of only ~2300-2800
    tokens — a prompt-size/instruction-following-under-load problem, not
    an ambiguity one, so a SECOND, short reminder positioned right before
    generation (same recency-anchoring trick as history_summary's own
    "do not restate" reminder) survives regardless of how large everything
    earlier in the prompt has grown."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    question = HumanMessage(content="what is a checkpointer?")
    state = {
        "messages": [question],
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert isinstance(seen[-1], SystemMessage)
    assert "bracket marker" in seen[-1].content
    # The bulk context TEXT stays anchored before the question (unchanged,
    # cache-stable); only this short reminder is recency-weighted.
    assert seen[0] is not seen[-1]
    assert "checkpointers persist" in seen[0].content


def test_agent_skips_the_citation_reminder_when_context_is_empty():
    """A general-knowledge or calculator-only answer has nothing to cite —
    SYSTEM_PROMPT explicitly allows that, so a reminder about "the
    retrieved content above" would be actively confusing when there is
    none."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    asyncio.run(agent({"messages": [HumanMessage(content="what is 2+2?")], "context": ""}))

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)


def test_citation_reminder_is_the_very_last_message_when_both_reminders_fire():
    """Recency ordering matters: the citation reminder is what actually
    drives retry_output's repair loop, so it gets the STRONGEST weighting
    of the two tail reminders — placed after history_summary's own."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [HumanMessage(content="what did we discuss earlier?")],
        "history_summary": "Earlier, the user asked about refund policy.",
        "context": "doc: checkpointers persist graph state.",
        "context_anchor_index": 0,
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert "do not restate" in seen[-2].content
    assert "bracket marker" in seen[-1].content


def test_agent_appends_a_sandbox_reminder_after_a_sandbox_requiring_skill_loads():
    """Real bug, found live via Langfuse (trace `633eee2b`, 2026-09-08):
    the deal-economics skill was loaded, its own body already says to use
    run_python_in_sandbox rather than estimate by hand, and the model
    computed the answer freehand anyway — the instruction was buried in a
    large tool-result chunk that got pushed further from the generation
    point as the turn went on. Same recency-anchoring fix as the citation
    reminder above: a short, tail-appended line, not a bigger rewrite of
    the skill's own body."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [
            HumanMessage(content="What's the real contract value?"),
            AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {}, "id": "c1"}]),
            ToolMessage(
                content="write a short script and run it with run_python_in_sandbox instead",
                tool_call_id="c1",
                name="use_skill",
            ),
        ],
    }
    asyncio.run(agent(state))

    seen = fake_llm.seen_messages
    assert isinstance(seen[-1], SystemMessage)
    assert "run_python_in_sandbox" in seen[-1].content


def test_agent_skips_the_sandbox_reminder_once_the_tool_was_actually_called():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [
            HumanMessage(content="What's the real contract value?"),
            AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {}, "id": "c1"}]),
            ToolMessage(
                content="write a short script and run it with run_python_in_sandbox instead",
                tool_call_id="c1",
                name="use_skill",
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "run_python_in_sandbox", "args": {}, "id": "c2"}],
            ),
            ToolMessage(content="141862.50", tool_call_id="c2", name="run_python_in_sandbox"),
        ],
    }
    asyncio.run(agent(state))

    assert not any(
        isinstance(m, SystemMessage) and "run_python_in_sandbox" in m.content
        for m in fake_llm.seen_messages
    )


def test_agent_skips_the_sandbox_reminder_when_no_skill_mentions_the_tool():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    agent = make_agent_node(fake_llm)

    state = {
        "messages": [
            HumanMessage(content="Help with my ticket."),
            AIMessage(content="", tool_calls=[{"name": "use_skill", "args": {}, "id": "c1"}]),
            ToolMessage(
                content="check the knowledge base first, then open a ticket",
                tool_call_id="c1",
                name="use_skill",
            ),
        ],
    }
    asyncio.run(agent(state))

    assert not any(isinstance(m, SystemMessage) for m in fake_llm.seen_messages)
