"""What a thread's checkpointed messages say about its most recent turn, for the queue worker that decides whether a reclaimed job is safe to retry
(`agent_worker._classify_reclaimed_turn`).

The worker owns the decision and its Redis side; reading a turn out of the messages needs to know which of them are the person's and which are the
graph's retry artifacts (`graph/messages.py`), and an inbound adapter reaches the engine through `runtime/` and not through `graph/`
(`tests/core/test_architecture_layers.py`). Pure: no I/O.
"""
from langchain_core.messages import AIMessage

from app.agent.graph.messages import is_retry_artifact, is_user_message


def last_turn_messages(messages: list) -> list | None:
    """The messages after the last one the PERSON sent, or `None` if the thread has none (it never reached its first turn).

    The turn's own question, not `retry_output`'s feedback (a HumanMessage too, tagged): see messages.is_user_message. Today the two readings agree,
    because the feedback and the rejected answer are written together; this keeps them agreeing if that ever changes."""
    last_human_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])),
        None,
    )
    if last_human_index is None:
        return None
    return messages[last_human_index + 1 :]


def turn_already_completed(turn_messages: list) -> bool:
    """True if the last message in this turn's own slice is a real final
    `AIMessage` (content produced, no pending `tool_calls`) — i.e. the
    graph already reached `check_output`/`END` (or a safety-net fallback)
    for this turn, which means `runtime/stream.py::_record_turn_metrics`
    already ran and recorded this turn's usage/cost. Retrying it — fresh
    OR continued — would re-run/re-tally an answer that already exists,
    silently double-recording that cost for no benefit; a zero-tool-call
    turn that crashed in the thin window between finishing and this job's
    own ack would otherwise look identical to one that never got that far
    at all."""
    if not turn_messages:
        return False
    last = turn_messages[-1]
    # A rejected answer (tagged by `retry_output`, see messages.RETRY_ARTIFACT_KEY) has no
    # tool calls either, but it is the answer the turn is about to be asked to replace.
    return isinstance(last, AIMessage) and not last.tool_calls and not is_retry_artifact(last)
