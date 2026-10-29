"""What a stopped turn leaves in the thread (GRAPH_PATTERNS.md pattern 80).

`POST /chat/cancel` stops a running turn, but the thread already holds the person's message (`validate_input`
wrote it) and nothing took it out; the stop was only accounted for (`stream._record_unfinished_turn`). The
next turn was built on a thread with two unanswered messages, and the model, which sees the thread and not the
click, answered the OLDER one (Langfuse trace `d6a00248`: "run it through a script" was stopped, then "Check
our API docs page ..." was sent, and the agent proposed the script and never touched the docs question).

A stop means "that did not happen", so by default the whole turn is removed from the thread. The exception is
a turn in which a tool had already RUN: its result is a fact the model must keep (a note was added, a ticket
filed; forgetting it invites doing it again under a new call id, which the exactly-once guard can't catch).
That turn stays and is closed with a short assistant line saying the person stopped it, which also answers the
dangling question.
"""
import asyncio
import logging

from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage, ToolMessage
from langgraph.graph import START

from app.agent.graph.messages import current_turn_messages, is_user_message
from app.core import metrics

logger = logging.getLogger(__name__)

# What the thread says about a turn that was stopped after a tool had run. The model reads it as its own
# answer to that turn's question, so it does not take the question up again unless the person repeats it.
STOPPED_TURN_MESSAGE = "[The person stopped this request before it finished.]"

_STATE_TIMEOUT_SECONDS = 2.0


async def discard_cancelled_turn(graph, cfg) -> str:
    """Take the turn the person stopped out of the thread, or close it. Returns what was done:
    `dropped`, `closed` (a tool had run), `nothing` (no turn in the thread), or `failed`.

    Never raises and never waits long: this runs while a turn is already ending, and the person's
    "stopped" must reach them whether or not the thread could be tidied. A failure leaves the thread
    as it was (the old behaviour) and says so in the log and the counter."""
    try:
        snapshot = await asyncio.wait_for(graph.aget_state(cfg), timeout=_STATE_TIMEOUT_SECONDS)
        messages = list(snapshot.values.get("messages") or []) if snapshot is not None and snapshot.values else []
        turn = current_turn_messages(messages)
        update: list[BaseMessage] | None
        if not turn or not is_user_message(turn[0]):
            outcome, update = "nothing", None
        elif any(isinstance(m, ToolMessage) for m in turn):
            outcome, update = "closed", [AIMessage(content=STOPPED_TURN_MESSAGE)]
        else:
            outcome, update = "dropped", [RemoveMessage(id=m.id) for m in turn if m.id]
        if update:
            # `as_node=START`: LangGraph would otherwise infer the node that wrote last, which is
            # "ambiguous" after a run that stopped part-way. START has one outgoing edge (to
            # `validate_input`) and no routing function to run on the edited thread, and the next
            # turn's input discards the tasks left pending (checked in the installed 0.2.76).
            await asyncio.wait_for(
                graph.aupdate_state(cfg, {"messages": update}, as_node=START), timeout=_STATE_TIMEOUT_SECONDS
            )
    except Exception as exc:  # noqa: BLE001 - tidying a stopped turn must never fail the stop itself
        logger.warning("cancelled_turn_not_discarded", extra={"error_class": type(exc).__name__})
        outcome = "failed"
    metrics.agent_cancelled_turn_discarded_total.labels(outcome=outcome).inc()
    return outcome
