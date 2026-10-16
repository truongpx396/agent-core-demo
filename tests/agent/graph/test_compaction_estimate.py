"""Compaction's token estimate counts what the model will be sent, not retry artifacts.

An earlier turn's rejected answer and the feedback it drew stay in the stored history, but
`agent` drops them before every call (messages.RETRY_ARTIFACT_KEY). Counting them made a history that
fits the budget look over it, so a paid summarization call and a trim ran earlier than they needed to.
"""
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.compaction import _estimate_tokens, _messages_to_trim
from app.agent.graph.messages import RETRY_ARTIFACT_KEY

TAGGED = {RETRY_ARTIFACT_KEY: True}


def _history_with_a_long_retry():
    return [
        HumanMessage(content="an early question about refunds", id="q0"),
        AIMessage(content="an early answer about refunds", id="a0"),
        HumanMessage(content="a later question about shipping", id="q1"),
        AIMessage(content="a rejected draft " * 200, id="a1r", additional_kwargs=TAGGED),
        HumanMessage(content="cite your sources " * 200, id="f1", additional_kwargs=TAGGED),
        AIMessage(content="a later answer about shipping", id="a1"),
        HumanMessage(content="the current question", id="q2"),
    ]


def test_retry_artifacts_add_nothing_to_the_estimate():
    messages = _history_with_a_long_retry()
    without = [m for m in messages if not m.additional_kwargs.get(RETRY_ARTIFACT_KEY)]

    assert _estimate_tokens(messages) == _estimate_tokens(without)


def test_a_history_that_fits_only_without_its_retry_artifacts_is_not_trimmed():
    messages = _history_with_a_long_retry()
    visible = [m for m in messages if not m.additional_kwargs.get(RETRY_ARTIFACT_KEY)]
    fits = _estimate_tokens(visible)  # a budget the model-visible history just meets

    assert _messages_to_trim(messages, ceiling=fits, floor=1) == []
