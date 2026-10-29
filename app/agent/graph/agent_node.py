"""`make_agent_node` — the node that calls the LLM, carrying the retrieved
context, the history summary and the skill reminders as text around the
turn's real question (`messages.with_turn_notes`, pattern 79).
"""
import logging

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.cost import metering
from app.agent.graph.links import LINK_REMINDER, mentions_link
from app.agent.graph.messages import (
    CANNOT_HELP_MESSAGE,
    GATEWAY_REFUSAL_KEY,
    WITHHELD_MESSAGE,
    current_turn_messages,
    human_text,
    is_legacy_prompt,
    is_retry_artifact,
    is_user_message,
    last_human_message,
    with_turn_notes,
)
from app.agent.graph.model_route import routed_unreachable, tier_alias
from app.agent.graph.skill_guards import pending_skill_required_tool
from app.agent.graph.state import State
from app.core import gateway, metrics
from app.core.config import CHAT_MODEL
from app.core.untrusted import frame_untrusted
from app.ports.llm import ContentBlock

logger = logging.getLogger(__name__)

# Short reminders that go AFTER the question, where the model is about to generate. Each exists because a
# small model dropped the instruction when it was far from the generation point: qwen2.5:3b dropped the
# "mandatory" citation rule in prompts of only ~2300-2800 tokens (a prompt-SIZE problem, not ambiguity, so
# making SYSTEM_PROMPT's wording more emphatic would only push the typical prompt deeper into that danger
# zone), and regurgitated the history summary verbatim (see test_history_summary_injection_tells_the_model_
# not_to_restate_it_verbatim). The citation reminder is what drives retry_output's citation-repair loop.
SUMMARY_REMINDER = "Reminder: do not restate the earlier-conversation summary above verbatim in your answer."
# "Above" is true now: the retrieved content is in the same message, ahead of the question. The second
# sentence is for a chunk that passed the retrieval floor and does not help (Langfuse trace `e0ca1456`: the
# "support hours" fact for "what's the status of ticket #4471?" came back as a citation at the end of an
# error message): a reminder that only says "cite" is read by a small model as "use the context".
CITATION_REMINDER = (
    "Reminder: every sentence in your answer that uses a fact from the retrieved content above must end "
    "with that fact's [n] bracket marker. If none of that content helps answer the question, ignore it "
    "and cite nothing."
)


def _question_index(messages: list[BaseMessage], anchor: int | None) -> int | None:
    """Where the turn's question is in `messages`: `retrieve_context`'s anchor when it still points at
    one (it records the question's index and only appends happen after it), else the last message the
    person sent. None when the thread has none."""
    if isinstance(anchor, int) and 0 <= anchor < len(messages) and is_user_message(messages[anchor]):
        return anchor
    return next((i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])), None)


def _refusal_update(state: State, block: ContentBlock) -> dict:
    """The turn's answer when the gateway's content guardrail refused the call.

    The refusal is flagged (`GATEWAY_REFUSAL_KEY`) so `should_continue` ends the turn on it. If the
    guardrail refused the REQUEST (`pre_call`), the person's message is replaced in the stored thread:
    the gateway scans every message on every call, so a refused message left in the thread refuses
    every later turn too (Langfuse trace `2c8e76a4`: "hey" was refused for an earlier message's
    "stock + should i"). Only this turn's message is replaced, the one just sent; the guardrail does
    not say which message matched, so a thread whose OLDER messages already match (a guardrail added
    after they were written) stays refused. A `post_call` block refused what the model wrote, so the
    question stays. The replacement keeps the message's id, which is how the reducer replaces it
    instead of appending a second one."""
    refusal = AIMessage(content=CANNOT_HELP_MESSAGE, additional_kwargs={GATEWAY_REFUSAL_KEY: True})
    messages: list = [refusal]
    asked = last_human_message(state["messages"]) if block.mode == "pre_call" else None
    if asked is not None and asked.id is not None:
        withheld = HumanMessage(
            content=WITHHELD_MESSAGE, id=asked.id, additional_kwargs=dict(asked.additional_kwargs)
        )
        messages.insert(0, withheld)
    return {"messages": messages, "iterations": state.get("iterations", 0) + 1}


# --- Node: agent ---
def make_agent_node(
    llm,
    model_alias: str = CHAT_MODEL,
    kind: str = "chat",
    citation_reminder: bool = True,
    skill_required_tools: tuple[str, ...] = (),
    cannot_open_links: bool = False,
    *,
    system_prompt: str,
):
    """Factory, not a plain function, because `agent` needs an LLM client; tests build it with a fake (e.g.
    GenericFakeChatModel) via `make_agent_node(fake_llm)(state)` to cover message assembly without a network
    call (test_agent_node.py).

    - `model_alias`: the LiteLLM alias `llm` is bound to, used only to price its calls. `build_graph` resolves
      it (`GraphDeps.model_alias`, else the configured chat alias); a direct caller passes its own.
    - `citation_reminder=False` (a nested subagent run) drops the tail "every sentence must end with a `[n]`
      marker" reminder: a subagent's prompt says the opposite (its markers would name ITS sources, meaningless
      to the parent), and a model handed both rules obeys neither. Retrieved context is still passed, framed
      as untrusted data.
    - `skill_required_tools`: the subset of `SKILL_REQUIRED_TOOL_MARKERS` the calling domain really has; the
      "call it now" reminder is only ever about one of those.
    - `cannot_open_links`: True for a domain with no tool that reaches an address (`links.cannot_open_links`);
      a turn whose question contains a link then carries a reminder that it can't be opened.
    - `system_prompt`: the calling domain's (`manifest.system_prompt`, bound by `build_graph`), the first
      message of EVERY call, taken from the manifest at call time rather than from the thread. Nothing
      persists a prompt in the checkpoint, so a prompt edit reaches every conversation on the next call with
      no migration, and a graph built directly is never an un-guided agent because nobody seeded it. Pass `""`
      only to unit-test message assembly.
    """

    async def agent(state: State, config: RunnableConfig | None = None) -> dict:
        """Call the LLM with the retrieved context carried by the turn's question.

        The domain's system prompt goes first on every call (see `make_agent_node`);
        beyond it this adds the per-turn retrieved context/history summary/reminders to the
        question itself, never as further system messages (pattern 79).

        `async def`/`ainvoke`, not `.invoke()`: this is the dominant
        per-turn cost (its own span ran ~3x longer than the GENERATION call
        inside it, under a 50-concurrent-turn Langfuse trace) and is real
        HTTP I/O, not CPU work. Before this, every concurrent turn's
        `agent` call held a thread in LangChain's shared, process-wide
        default executor (16 threads in this deployment) for its ENTIRE
        duration, capping concurrent LLM calls at that count regardless of
        AGENT_WORKER_MAX_CONCURRENCY. `ChatOpenAI.ainvoke()` is a real
        async client (not `.invoke()` run in a thread); GenericFakeChatModel
        (tests) still works via BaseChatModel's default `ainvoke`.
        """
        messages = list(state["messages"])
        anchor = state.get("context_anchor_index")
        history_summary = state.get("history_summary", "")
        # History the model must not see. (1) A prompt persisted in the thread by an older build
        # (`is_legacy_prompt`): the current one is prepended below instead, and replaying the old
        # one would give the model two, possibly contradicting. (2) Earlier turns' retry artifacts
        # (a rejected answer and the feedback it drew, see messages.RETRY_ARTIFACT_KEY): replayed,
        # the feedback reads as something the person said. THIS turn's artifacts stay, because the
        # retry in progress needs them. Dropping shifts indexes, so `anchor` is re-located by
        # identity (the anchored message is the turn's own question, never one of these).
        turn_start = next(
            (i for i in range(len(messages) - 1, -1, -1) if is_user_message(messages[i])), None
        )

        def droppable(i: int, m) -> bool:
            if is_legacy_prompt(m):
                return True
            return turn_start is not None and i < turn_start and is_retry_artifact(m)

        if any(droppable(i, m) for i, m in enumerate(messages)):
            anchor_msg = (
                messages[anchor] if isinstance(anchor, int) and 0 <= anchor < len(messages) else None
            )
            messages = [m for i, m in enumerate(messages) if not droppable(i, m)]
            anchor = next((i for i, m in enumerate(messages) if m is anchor_msg), None)
        context = state.get("context", "")

        # What this call adds to the turn rides on the turn's own question: reference material
        # (the summary of earlier turns, then the retrieved context, most relevant closest to the
        # ask) as text before what the person wrote, and the short reminders after it, where they
        # are recency-weighted for real. Not as system messages, which is what this used to do and
        # what the comments here used to call "anchored" and "tail-appended": Ollama hoists every
        # system message into the one system slot at the top of the prompt, so none of them sat
        # near the question, and since they differ on every turn the whole history was re-read
        # every turn (`messages.with_turn_notes`, GRAPH_PATTERNS.md pattern 79). The question is
        # the same message on every call this turn makes, so a tool loop or a retry keeps the
        # same prefix. The thread itself is untouched: only the copy sent to the model carries
        # the notes.
        before: list[str] = []
        after: list[str] = []
        if history_summary:
            before.append(f"Summary of earlier conversation (background only):\n{history_summary}")
            after.append(SUMMARY_REMINDER)
        if context:
            # Untrusted content framing: retrieved text is data, never
            # instructions (the textbook prompt-injection defense) — the
            # delimiters plus the SYSTEM_PROMPT rule make this structural.
            # `frame_untrusted` is also what every tool that returns outside
            # text uses, and it stops the text closing its own frame.
            before.append(frame_untrusted(context))
            if citation_reminder:
                after.append(CITATION_REMINDER)

        pending_tool = pending_skill_required_tool(
            current_turn_messages(state["messages"]), skill_required_tools
        )
        if pending_tool:
            # Same recency-weighted reminder, for the SAME reason (see
            # pending_skill_required_tool's docstring): a loaded skill's
            # "use this tool, don't estimate by hand" instruction is buried
            # in tool-result text and gets pushed further from generation
            # each round. Names `pending_tool` itself (not hardcoded) so
            # this stays correct as SKILL_REQUIRED_TOOL_MARKERS grows.
            # Proactive, not a replacement for check_output's reactive
            # `skipped_required_sandbox_after_skill` catch — real bug
            # found live: the model ignored an equally direct reminder
            # appended to use_skill's own returned text once already, so no
            # single nudge is assumed sufficient.
            after.append(
                f"Reminder: call {pending_tool} now, for real, for the "
                "computation the skill you loaded described — do not "
                "compute the result yourself."
            )

        if cannot_open_links and mentions_link(human_text(last_human_message(state["messages"]))):
            # Last, so it is the closest thing to generation: measured on the 7b, a reminder behind the
            # citation reminder was obeyed where the same sentence in the system prompt was not.
            after.append(LINK_REMINDER)

        if before or after:
            asked = _question_index(messages, anchor)
            question = messages[asked] if asked is not None else None
            if asked is not None and isinstance(question, HumanMessage):
                messages[asked] = with_turn_notes(question, before, after)
            else:
                # No question in the thread (a hand-built test State): the notes are all there is.
                messages.append(HumanMessage(content="\n\n".join([*before, *after])))

        if system_prompt:
            messages.insert(0, SystemMessage(content=system_prompt))

        # The one place a call is made, priced and recorded (app/agent/cost/metering.py): it tells the gateway
        # whose call this is (else its spend logs and Langfuse show one anonymous caller), prices the usage
        # once from the call's own input/output/cached split by the model this node's client talks to
        # (`model_alias` for a delegated run whose specialist declares one, else the global chat alias), and
        # writes one usage event. The in-run ceiling below and the event come from that one pricing, so the
        # ceiling and the bill can't disagree (pattern 35).
        #
        # A turn the router sent to the medium or strong tier (`route_model`, specs/014, 015) calls that
        # tier's alias instead, and is priced and recorded as the alias that served; every other turn makes
        # exactly the call it always did. If the routed call never reaches a model, THAT ONE CALL is repeated
        # on the local alias (the next call tries the routed alias again). This lives here and not in the
        # gateway's `fallbacks`, which would price and record the routed alias that did not answer.
        routed = tier_alias(state.get("model_route"))
        try:
            if routed is None:
                call = await metering.metered_invoke(
                    llm,
                    messages,
                    config=config,
                    kind=kind,
                    model_alias=model_alias,
                )
            else:
                try:
                    call = await metering.metered_invoke(
                        llm, messages, config=config, kind=kind, model_alias=routed, invoke_model=routed
                    )
                except Exception as exc:  # noqa: BLE001 - classified by routed_unreachable; anything that is not "never reached a model" is re-raised unchanged
                    if not routed_unreachable(exc):
                        raise
                    metrics.agent_model_route_total.labels(outcome="routed_unreachable_fell_back").inc()
                    logger.warning(
                        "routed model unreachable; repeating the call on the local model",
                        extra={"alias": routed, "error_class": type(exc).__name__, "run_id": state.get("run_id", "-")},
                    )
                    call = await metering.metered_invoke(
                        llm,
                        messages,
                        config=config,
                        kind=kind,
                        model_alias=model_alias,
                    )
        except Exception as exc:  # noqa: BLE001 - only the gateway's content-guardrail refusal becomes the turn's answer; every other failure is re-raised unchanged for the retry policy and the stream to classify
            block = gateway.note_content_block(exc)
            if block is None:
                raise
            return _refusal_update(state, block)
        response, usage = call.response, call.usage
        if (
            usage.get("output_tokens")
            and not response.content
            and not response.tool_calls
            and not response.invalid_tool_calls
        ):
            # The provider generated something and handed back nothing: not text, not a tool call, not
            # even a malformed one (those arrive as `invalid_tool_calls`). Seen with Ollama dropping a
            # tool call it could not parse. The graph reads this as an empty reply, so say it happened.
            metrics.agent_unparsed_completion_total.inc()
            logger.warning(
                "model produced output tokens but no text and no tool call",
                extra={"output_tokens": usage.get("output_tokens"), "run_id": state.get("run_id", "-")},
            )

        # usage_metadata is populated only when the model/proxy reports it
        # (not guaranteed) — missing usage just means the budget never trips.
        turn_tokens = usage.get("total_tokens", 0)
        total_tokens = state.get("total_tokens", 0) + turn_tokens

        # Cost ceiling bookkeeping (pattern 35), applied HERE incrementally so
        # should_continue can stop the run before the NEXT call.
        total_cost_usd = state.get("total_cost_usd", 0.0) + call.cost_usd

        return {
            "messages": [response],
            "iterations": state.get("iterations", 0) + 1,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
        }

    return agent
