"""Retry/give-up node family: `retry_output` (send the agent back with corrective feedback naming
the SPECIFIC rejection reason), `make_retry_exhausted_node` (route_after_check giving up on a stuck
retry loop — MAX_CONSECUTIVE_SAME_RETRY_REASON), and `make_no_answer_fallback_node` (should_continue's
own four safety-net exits — max iterations/tokens/cost, no-progress). The reasons, their feedback and
their fallback text are the registry in `graph/output_checks.py`.
"""
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.graph.messages import RETRY_ARTIFACT_KEY, reject_tool_calls
from app.agent.graph.output_checks import check_for, fallback_message, trusts_content
from app.agent.graph.routing import check_output
from app.agent.graph.state import State
from app.core import metrics


def _as_retry_artifact(message: BaseMessage | None) -> list[BaseMessage]:
    """`message` re-tagged as a retry artifact, to put in a node's return value, or `[]` when it
    can't safely be: only a plain answer (an AIMessage with no `tool_calls`) that already has an
    id. The copy keeps that id, so `add_messages` REPLACES the message in the thread rather than
    appending a second one. An AIMessage with `tool_calls` is never tagged, because the readers
    that skip artifacts would then drop it and orphan its ToolMessages; a message with no id
    (a hand-built test state, never a checkpointed one) would be appended as a duplicate."""
    if not isinstance(message, AIMessage) or message.tool_calls or message.id is None:
        return []
    return [
        message.model_copy(
            update={"additional_kwargs": {**message.additional_kwargs, RETRY_ARTIFACT_KEY: True}}
        )
    ]


def retry_output(state: State) -> dict:
    """Send the agent back with corrective feedback instead of looping on
    the same messages; MAX_ITERATIONS in should_continue still bounds the
    total number of retries.

    The feedback names the SPECIFIC rejection reason `check_output` recorded
    (`State.last_retry_reason`, with its detail from `output_findings`),
    rather than a generic "try again": each reason's text lives with its check
    in `graph/output_checks.py`, which also decides which reason wins when
    several apply. There is no default here: a retry nobody can explain is a
    bug, so an unknown or missing reason raises.
    """
    metrics.agent_retry_total.inc()
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    reason = state.get("last_retry_reason")
    if reason is None:
        raise ValueError("retry_output is only reached when check_output recorded a reason")
    feedback = check_for(reason).feedback((state.get("output_findings") or {}).get(reason))
    # Both messages are tagged so that nothing downstream mistakes them for conversation: the
    # feedback is not something the person said, and the rejected answer is not one they were
    # shown (pattern 5). The agent node still sees them for the rest of THIS turn, which is
    # what makes the retry work.
    return {
        "messages": [
            *_as_retry_artifact(last),
            HumanMessage(content=feedback, additional_kwargs={RETRY_ARTIFACT_KEY: True}),
        ]
    }


# --- Node: retry loop gave up — reached via route_after_check when the SAME
# rejection reason repeats MAX_CONSECUTIVE_SAME_RETRY_REASON times in a row.
# Sibling of no_answer_fallback below (same emit_message-gated shape for
# run_subagent), but check_output HAS already judged this content here, so
# trust is reason-dependent (OutputCheck.trust_on_exhaust) rather than
# always-trusted the way no_answer_fallback's orphaned content is. ---
def make_retry_exhausted_node(emit_message: bool = True):
    def retry_exhausted(state: State) -> dict:
        last = state["messages"][-1] if state.get("messages") else None
        content = getattr(last, "content", "") or ""
        if trusts_content(state.get("last_retry_reason")) and (isinstance(content, str) and content.strip()):
            # No-op — check_output's own most recent computation already
            # reflects this exact content; nothing to override.
            return {}
        if emit_message:
            content = fallback_message(state.get("last_retry_reason"))
        else:
            # Silenced for run_subagent: NOT a no-op like the trusted
            # branch above — content here is untrusted, so it's blanked
            # rather than kept, letting run_subagent's own "is final
            # content non-empty" check correctly treat this as no answer
            # produced (outcome="budget_exceeded").
            content = ""
        return {
            # The answer being replaced was judged untrustworthy; leave it out of the transcript.
            "messages": [*_as_retry_artifact(last), AIMessage(content=content)],
            "used_citations": [],
            "ungrounded_claims_count": 0,
        }

    return retry_exhausted


# --- Node: top-level "no answer" fallback — reached only via should_continue's
# four safety-net exits (max iterations/tokens/cost, no-progress), never
# from check_output's normal path — so state["messages"][-1] may be an
# empty AIMessage (a small model can burn all its tokens failing to produce
# real content or a valid tool call). Without this node, that empty message
# would be the turn's final answer, and
# runtime/stream.py::_run_graph_stream's own streaming fallback reads
# exactly this last message to synthesize a token event.
#
# emit_message=False (run_subagent's nested graphs) makes this a no-op:
# run_subagent (app/agent/tools/builtin.py) already runs the IDENTICAL "empty final
# AIMessage" check on its own terms to report outcome="budget_exceeded" —
# filling in prose here would make that check wrongly see real text and
# report the run as completed. ---
def make_no_answer_fallback_node(
    emit_message: bool = True,
    *,
    system_prompt: str,
    enforce_citations: bool = True,
    skill_required_tools: tuple[str, ...] = (),
):
    def no_answer_fallback(state: State) -> dict:
        last = state["messages"][-1]
        # A safety net can trip on the very round whose AIMessage is asking for tools. Those calls
        # will never run, and a call with no ToolMessage fails the next LLM call on this thread
        # (OpenAI-style APIs reject it), so each one is answered here — also for a subagent
        # (emit_message=False), whose thread is discarded but whose graph must not end on a
        # malformed history either. `run_subagent` still reads the last AIMessage, which stays
        # the empty tool-call one, so it still reports "budget_exceeded".
        pending_calls = (last.tool_calls or []) if isinstance(last, AIMessage) else []
        rejections = reject_tool_calls(
            pending_calls,
            "Not run: this turn reached a safety limit before this call could run.",
        )
        if not emit_message:
            return {"messages": rejections} if rejections else {}
        content = getattr(last, "content", "") or ""
        # Freshly runs check_output's OWN validity logic on THIS content,
        # not a stale state["last_retry_reason"] — should_continue routed
        # here via a safety-net exit, so check_output never ran on this
        # round at all. Real bug, found live: this used to trust any
        # non-blank content unconditionally, silently bypassing every
        # check_output safety check (leaked prompt, fabricated output,
        # etc.) whenever a budget tripped on the same round that produced
        # bad content — an unvetted narrated deferral reached the user
        # this way (Langfuse trace `633eee2b`, 2026-09-08). Recomputing
        # fresh here means every current AND future check_output check
        # automatically applies.
        fresh = check_output(
            state,
            system_prompt=system_prompt,
            enforce_citations=enforce_citations,
            skill_required_tools=skill_required_tools,
        )
        # Text written alongside a tool request is the model narrating what it is about to do, not
        # a final answer, so it is never shown even when it would pass check_output.
        trustworthy = (
            not pending_calls
            and trusts_content(fresh["last_retry_reason"])
            and isinstance(content, str)
            and bool(content.strip())
        )
        updates: dict = {
            "used_citations": fresh["used_citations"],
            "ungrounded_claims_count": fresh["ungrounded_claims_count"],
        }
        if not trustworthy:
            content = fallback_message(fresh["last_retry_reason"])
            # The untrusted answer being replaced stays in the thread, but as an artifact.
            updates["messages"] = [*rejections, *_as_retry_artifact(last), AIMessage(content=content)]
        elif "messages" in fresh:
            # check_output may have mechanically inserted a missing
            # citation marker into this content
            # (insert_missing_citation_markers) — carry that correction
            # through, or `used_citations` above would claim a marker the
            # message actually shown to the user doesn't contain.
            updates["messages"] = fresh["messages"]
        # followups is deliberately NOT computed here — suggest_followups
        # needs its own LLM call, and generating more content right after
        # an over-budget cutoff defeats the point; citations are free,
        # computed over content already paid for.
        return updates

    return no_answer_fallback
