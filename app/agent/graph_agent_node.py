"""`make_agent_node` — the node that actually calls the LLM, injecting
retrieved context/history-summary/skill reminders as extra SystemMessages
around the turn's real question. Split out of `app/agent/graph.py` purely
for file size — see that module's own docstring, and
`app/agent/graph_retrieval.py`/`app/agent/graph_cache.py` for the sibling
splits. No behavior change from the pre-split single-file version.

Reads `CHAT_MODEL` through `graph_module.CHAT_MODEL` rather than a plain
statically-imported bare name — same real bug/fix as
`app/agent/graph_utils.py`'s own `graph_module.CHAT_MODEL` (see its
docstring): several `tests/live/*`/`tests/deepeval/*` files do
`monkeypatch.setattr(graph_module, "CHAT_MODEL", ...)`, patching an
attribute on the live `app.agent.graph` module object. A statically-
imported bare name here would bind to the ORIGINAL value once, at this
module's own import time, permanently, so the monkeypatch would silently
never take effect.
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
    `make_agent_node(fake_llm)(state)` — see tests/agent/test_agent_node.py — so the
    node's message-assembly logic (context injection, iteration bump) is
    covered without a network call to a real model.
    """

    async def agent(state: State) -> dict:
        """Call the LLM, injecting retrieved context as an extra SystemMessage.

        The *base* SYSTEM_PROMPT is seeded once per thread by
        app/agent/runtime.py::_ensure_seeded_async before the graph ever runs, so we don't
        repeat it here — we only add the per-turn retrieved context, which
        actually needs to reach the model on every agent step.

        `async def`/`ainvoke` (not `.invoke()`): this is the dominant
        per-turn cost (verified via a real Langfuse trace under a 50-
        concurrent-turn burst — this node's own span ran ~3x longer than
        the GENERATION call inside it) and a real HTTP call — it's I/O, not
        CPU work, so it belongs on the event loop. Before this, every
        concurrent turn's `agent` call held a thread in LangChain's shared,
        process-wide default executor (`min(32, os.cpu_count()+4)`, 16 in
        this deployment) for its ENTIRE duration, capping concurrent LLM
        calls at that thread count regardless of AGENT_WORKER_MAX_CONCURRENCY
        or how many are actually in flight against the model/proxy.
        `langchain_openai.ChatOpenAI.ainvoke()` is a real async client
        (httpx/openai's AsyncOpenAI under the hood), not `.invoke()` run in
        a thread — GenericFakeChatModel (tests/agent/test_agent_node.py)
        still works unchanged: langchain_core.language_models.BaseChatModel
        provides a default `ainvoke` for any model that only implements the
        sync path.
        """
        messages = list(state["messages"])
        anchor = state.get("context_anchor_index")
        history_summary = state.get("history_summary", "")
        context = state.get("context", "")

        # Both history_summary and context are front-loaded together, right
        # BEFORE the turn's own question, at the SAME fixed position on
        # every call this turn makes (retrieve_context computed `anchor`
        # once, before the agent<->tools/check_output<->retry_output loop
        # started appending anything after it) — not appended at whatever
        # the CURRENT tail happens to be, which would put them at a
        # DIFFERENT relative position on every subsequent call as
        # tool-calls/retries pile up after them. Bulk reference content
        # belongs up front for two independent reasons at once: a
        # provider's/inference engine's prefix-cache reuse (Ollama/
        # llama.cpp included) needs a stable, unchanging prefix, AND the
        # common convention for long reference material is to place it
        # before the immediate ask, not after (Anthropic's own long-context
        # guidance says the same for retrieved documents). Summary before
        # context: more general background first, the currently-relevant
        # retrieved docs closest to the question itself. See
        # State.context_anchor_index's own docstring.
        prefix_inserts: list[SystemMessage] = []
        if history_summary:
            prefix_inserts.append(
                SystemMessage(
                    content=f"Summary of earlier conversation (background only):\n{history_summary}"
                )
            )
        if context:
            # Untrusted content framing: retrieved text is data, never
            # instructions (a document saying "ignore previous instructions"
            # is the textbook prompt-injection vector) — the delimiters plus
            # the SYSTEM_PROMPT rule are what make that structural rather
            # than something the model has to remember to apply itself.
            prefix_inserts.append(
                SystemMessage(
                    content=f"<retrieved_document>\n{context}\n</retrieved_document>"
                )
            )
        if prefix_inserts:
            if isinstance(anchor, int) and 0 <= anchor < len(messages):
                messages[anchor:anchor] = prefix_inserts
            else:
                # No anchor to work with (e.g. a hand-built test State
                # that never ran retrieve_context) — fall back to the old
                # tail-append rather than guessing at a position.
                messages.extend(prefix_inserts)

        if history_summary:
            # A SHORT, standalone reminder appended at the CURRENT tail —
            # deliberately NOT anchored like the summary text above. A real
            # regression (see
            # test_history_summary_injection_tells_the_model_not_to_restate_it_verbatim)
            # showed a small model regurgitating the summary when this
            # instruction wasn't close enough to the generation point.
            # Splitting it out keeps only this one line — not the whole
            # summary block — recency-weighted: the anti-regurgitation
            # protection survives at a near-zero cache-stability cost,
            # since the bulk of the summary TEXT now lives in the stable,
            # anchored prefix above and only this short reminder's position
            # shifts turn to turn.
            messages.append(
                SystemMessage(
                    content=(
                        "Reminder: do not restate the earlier-conversation "
                        "summary above verbatim in your answer."
                    )
                )
            )

        if context:
            # Same recency-anchoring fix as history_summary's own reminder
            # above, for the SAME failure mode SYSTEM_PROMPT's own citation
            # rule already documents: verified live via Langfuse that
            # qwen2.5:3b drops this "mandatory" instruction in prompts of
            # only ~2300-2800 tokens — a prompt-SIZE/instruction-following-
            # under-load problem, not an ambiguity one, so making
            # SYSTEM_PROMPT's own wording MORE emphatic would only push
            # typical prompt size deeper into that same danger zone
            # (confirmed the OPPOSITE direction too: adding one new
            # unrelated paragraph to SYSTEM_PROMPT measurably increased
            # citation-retry frequency on otherwise-tiny prompts). A short,
            # tail-appended line survives regardless of how large
            # everything BEFORE it has grown, at near-zero cache-stability
            # cost — only this one line's position shifts turn to turn,
            # same tradeoff history_summary's reminder above already
            # makes. Placed AFTER that reminder (closest to generation):
            # this is the instruction actually driving retry_output's
            # citation-repair loop, so it gets the strongest recency
            # weighting of the two.
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
            # reminders above, for the SAME underlying reason
            # (_pending_skill_required_tool's own docstring): a loaded
            # skill's own "use this tool, don't estimate by hand"
            # instruction lives buried in a big chunk of tool-result text,
            # and gets pushed further from the generation point every
            # subsequent round a turn takes (a failed script, an error
            # message, a narrated retry) — this is a SHORT, standalone
            # line placed as close to generation as possible instead,
            # surviving regardless of how large everything before it has
            # grown. Names `pending_tool` itself, not a hardcoded tool
            # name, so this stays correct if _SKILL_REQUIRED_TOOL_MARKERS
            # ever grows past run_command_in_sandbox. Proactive, not a
            # replacement for the reactive catch: check_output's own
            # _skipped_required_sandbox_after_skill still fires afterward
            # if this doesn't work either — real bug, found live, the
            # model ignored an equally direct reminder appended to
            # use_skill's OWN returned text (app/agent/tools.py) once
            # already, so no single nudge is assumed sufficient on its
            # own.
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

        # Token budget bookkeeping: usage_metadata is populated when the
        # underlying model/proxy reports it (not guaranteed — e.g. depends
        # on Ollama/LiteLLM passing usage through). Missing usage just
        # means the token budget never trips, not an error.
        usage = getattr(response, "usage_metadata", None) or {}
        turn_tokens = usage.get("total_tokens", 0)
        total_tokens = state.get("total_tokens", 0) + turn_tokens

        # Cost ceiling bookkeeping (GRAPH_PATTERNS.md pattern 35) — same
        # price table app/agent/usage_ledger.py's post-hoc ledger uses, applied
        # HERE, incrementally, so should_continue can stop the run before
        # the NEXT call rather than only recording what the turn already
        # spent after it's over.
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
