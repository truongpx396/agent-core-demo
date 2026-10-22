"""`make_agent_node` — the node that calls the LLM, injecting retrieved
context/history-summary/skill reminders as extra SystemMessages around the
turn's real question.
"""
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.cost import metering
from app.agent.graph.messages import (
    CANNOT_HELP_MESSAGE,
    GATEWAY_REFUSAL_KEY,
    WITHHELD_MESSAGE,
    current_turn_messages,
    is_legacy_prompt,
    is_retry_artifact,
    is_user_message,
    last_human_message,
)
from app.agent.graph.model_route import strong_unreachable
from app.agent.graph.skill_guards import pending_skill_required_tool
from app.agent.graph.state import State
from app.core import gateway, metrics
from app.core.config import CHAT_MODEL, STRONG_CHAT_MODEL
from app.core.untrusted import frame_untrusted

logger = logging.getLogger(__name__)


def _refusal_update(state: State, block: "gateway.ContentBlock") -> dict:
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
    *,
    system_prompt: str,
):
    """Factory, not a plain function, because `agent` needs an LLM client.
    Tests build it with a fake (e.g. GenericFakeChatModel) via
    `make_agent_node(fake_llm)(state)` (see test_agent_node.py) to cover
    message-assembly logic without a network call.

    `model_alias` is the LiteLLM alias `llm` is bound to, used only to price its
    calls. `build_graph` resolves it (`GraphDeps.model_alias`, else the configured
    chat alias); a caller building the node directly passes its own.

    `citation_reminder=False` (a nested subagent run) leaves out the tail
    reminder that every sentence must end with a `[n]` marker. A subagent's
    prompt says the opposite (its markers would name ITS sources, meaningless
    to the parent), and a model handed both rules obeys neither. The retrieved
    context is still passed, framed as untrusted data.

    `skill_required_tools` is the subset of `SKILL_REQUIRED_TOOL_MARKERS` the calling domain
    really has; the "call it now" reminder is only ever about one of those.

    `system_prompt` is the calling domain's (`manifest.system_prompt`, bound by `build_graph`).
    It is the first message of EVERY call, taken from the manifest at call time rather than read
    out of the thread: nothing persists a prompt in the checkpoint, so editing a prompt reaches
    every conversation on the next call with no migration, and a graph built directly (a test, a
    script) is never an un-guided agent because nobody remembered to seed it. Pass `""` only to
    unit-test message assembly.
    """

    async def agent(state: State, config: RunnableConfig | None = None) -> dict:
        """Call the LLM, injecting retrieved context as an extra SystemMessage.

        The domain's system prompt goes first on every call (see `make_agent_node`);
        beyond it this adds the per-turn retrieved context/history summary.

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

        # history_summary and context are front-loaded together, right
        # BEFORE the turn's question, at the SAME fixed position on every
        # call this turn makes (`anchor` was computed once by
        # retrieve_context, before the loop started appending anything
        # after it) — not at the current tail, which would shift on every
        # subsequent call as tool-calls/retries pile up. Stable prefix
        # position matters for prefix-cache reuse AND is the standard
        # convention for placing bulk reference material before the ask.
        # Summary before context: general background first, most-relevant
        # docs closest to the question. See State.context_anchor_index.
        prefix_inserts: list[SystemMessage] = []
        if history_summary:
            prefix_inserts.append(
                SystemMessage(
                    content=f"Summary of earlier conversation (background only):\n{history_summary}"
                )
            )
        if context:
            # Untrusted content framing: retrieved text is data, never
            # instructions (the textbook prompt-injection defense) — the
            # delimiters plus the SYSTEM_PROMPT rule make this structural.
            # `frame_untrusted` is also what every tool that returns outside
            # text uses, and it stops the text closing its own frame.
            prefix_inserts.append(SystemMessage(content=frame_untrusted(context)))
        if prefix_inserts:
            if isinstance(anchor, int) and 0 <= anchor < len(messages):
                messages[anchor:anchor] = prefix_inserts
            else:
                # No anchor (e.g. a hand-built test State that never ran
                # retrieve_context) — fall back to tail-append.
                messages.extend(prefix_inserts)

        if history_summary:
            # Short, standalone reminder at the CURRENT tail — deliberately
            # NOT anchored like the summary text above. A real regression
            # (see test_history_summary_injection_tells_the_model_not_to_
            # restate_it_verbatim) showed a small model regurgitating the
            # summary when this instruction wasn't close to the generation
            # point. Keeping only this short line recency-weighted costs
            # near-zero cache stability, since the bulk text stays anchored.
            messages.append(
                SystemMessage(
                    content=(
                        "Reminder: do not restate the earlier-conversation "
                        "summary above verbatim in your answer."
                    )
                )
            )

        if context and citation_reminder:
            # Same recency-anchoring fix as history_summary's reminder
            # above, for the SAME failure mode: qwen2.5:3b has been caught
            # dropping this "mandatory" citation instruction in prompts of
            # only ~2300-2800 tokens — a prompt-SIZE problem, not
            # ambiguity, so making SYSTEM_PROMPT's own wording more
            # emphatic would only push typical prompt size deeper into
            # that danger zone (confirmed: one new unrelated paragraph
            # there measurably increased retry frequency on tiny prompts).
            # Placed AFTER the history_summary reminder — closest to
            # generation, since this is what actually drives
            # retry_output's citation-repair loop.
            messages.append(
                SystemMessage(
                    content=(
                        "Reminder: every sentence in your answer that uses "
                        "a fact from the retrieved content above must end "
                        "with that fact's [n] bracket marker."
                    )
                )
            )

        pending_tool = pending_skill_required_tool(
            current_turn_messages(state["messages"]), skill_required_tools
        )
        if pending_tool:
            # Same recency-weighted, tail-appended mechanism as the two
            # reminders above, for the SAME reason (see
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
            messages.append(
                SystemMessage(
                    content=(
                        f"Reminder: call {pending_tool} now, for real, for the "
                        "computation the skill you loaded described — do not "
                        "compute the result yourself."
                    )
                )
            )

        if system_prompt:
            messages.insert(0, SystemMessage(content=system_prompt))

        # The one place a call is made, priced and recorded (app/agent/cost/metering.py): it tells the
        # gateway whose call this is (its spend logs and Langfuse are otherwise one anonymous
        # caller), prices the usage once from the call's own input/output/cached split by the
        # model this node's client actually talks to — `model_alias` for a delegated run whose
        # specialist declares its own model, else the global chat alias — and writes one usage
        # event. The in-run ceiling below and the event all come from that one pricing, so the ceiling and the bill can never
        # disagree about what a turn cost (pattern 35).
        #
        # A turn the router sent to the stronger model (`route_model`, specs/014) calls the strong alias in place of this client's own,
        # and is priced and recorded as the alias that served. Every other turn makes exactly the call it always did, with no extra
        # keyword. If the strong call never reaches a model, THAT ONE CALL is repeated on the local alias: the ledger then names the local
        # model (a call that failed records nothing), and the next call this turn tries the strong alias again. This lives here and not in
        # the gateway's `fallbacks` because a gateway fallback would be priced and recorded as the strong alias that did not answer.
        strong = STRONG_CHAT_MODEL if state.get("model_route") == "strong" and STRONG_CHAT_MODEL else None
        try:
            if strong is None:
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
                        llm, messages, config=config, kind=kind, model_alias=strong, invoke_model=strong
                    )
                except Exception as exc:  # noqa: BLE001 - classified by strong_unreachable; anything that is not "never reached a model" is re-raised unchanged
                    if not strong_unreachable(exc):
                        raise
                    metrics.agent_model_route_total.labels(outcome="strong_failed_fell_back").inc()
                    logger.warning(
                        "strong model unreachable; repeating the call on the local model",
                        extra={"alias": strong, "error_class": type(exc).__name__, "run_id": state.get("run_id", "-")},
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
