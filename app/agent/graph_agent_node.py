"""`make_agent_node` — the node that calls the LLM, injecting retrieved
context/history-summary/skill reminders as extra SystemMessages around the
turn's real question. Split out of graph.py purely for file size; see
graph_retrieval.py/graph_cache.py for sibling splits. No behavior change.

Reads `CHAT_MODEL` via `graph_module.CHAT_MODEL`, not a static import —
several tests/live and tests/deepeval files monkeypatch
`graph_module.CHAT_MODEL` directly, which a statically-imported bare name
would never see (binds once, at import time). Same pattern as
graph_utils.py's `_make_llm`.
"""
from langchain_core.messages import SystemMessage

from app.agent import graph as graph_module
from app.agent.graph import State
from app.agent.graph_skills import _pending_skill_required_tool
from app.agent.graph_tools import _current_turn_messages


# --- Node: agent ---
def make_agent_node(llm):
    """Factory, not a plain function, because `agent` needs an LLM client.
    Tests build it with a fake (e.g. GenericFakeChatModel) via
    `make_agent_node(fake_llm)(state)` (see test_agent_node.py) to cover
    message-assembly logic without a network call.
    """

    async def agent(state: State) -> dict:
        """Call the LLM, injecting retrieved context as an extra SystemMessage.

        The *base* SYSTEM_PROMPT is seeded once per thread by
        runtime.py::_ensure_seeded_async before the graph ever runs, so
        this only adds the per-turn retrieved context/history summary.

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
            prefix_inserts.append(
                SystemMessage(
                    content=f"<retrieved_document>\n{context}\n</retrieved_document>"
                )
            )
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

        if context:
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

        pending_tool = _pending_skill_required_tool(_current_turn_messages(state["messages"]))
        if pending_tool:
            # Same recency-weighted, tail-appended mechanism as the two
            # reminders above, for the SAME reason (see
            # _pending_skill_required_tool's docstring): a loaded skill's
            # "use this tool, don't estimate by hand" instruction is buried
            # in tool-result text and gets pushed further from generation
            # each round. Names `pending_tool` itself (not hardcoded) so
            # this stays correct as _SKILL_REQUIRED_TOOL_MARKERS grows.
            # Proactive, not a replacement for check_output's reactive
            # `_skipped_required_sandbox_after_skill` catch — real bug
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

        response = await llm.ainvoke(messages)

        # usage_metadata is populated only when the model/proxy reports it
        # (not guaranteed) — missing usage just means the budget never trips.
        usage = getattr(response, "usage_metadata", None) or {}
        turn_tokens = usage.get("total_tokens", 0)
        total_tokens = state.get("total_tokens", 0) + turn_tokens

        # Cost ceiling bookkeeping (pattern 35) — same price table
        # usage_ledger.py's post-hoc ledger uses, applied HERE
        # incrementally so should_continue can stop the run before the
        # NEXT call.
        from app.agent.usage_ledger import PRICE_PER_1K_TOKENS_USD

        price_per_1k = PRICE_PER_1K_TOKENS_USD.get(graph_module.CHAT_MODEL, 0.0)
        turn_cost = (turn_tokens / 1000) * price_per_1k
        total_cost_usd = state.get("total_cost_usd", 0.0) + turn_cost

        return {
            "messages": [response],
            "iterations": state.get("iterations", 0) + 1,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
        }

    return agent
