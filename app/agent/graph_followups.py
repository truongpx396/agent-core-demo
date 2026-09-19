"""`make_suggest_followups_node` — suggests 2-3 follow-up questions from a
grounded final answer (GRAPH_PATTERNS.md pattern 27). Split out of
`app/agent/graph.py` purely for file size — see that module's own
docstring. No behavior change from the pre-split single-file version.
"""
import logging

from langchain_core.messages import HumanMessage

from app.agent.graph import State

logger = logging.getLogger(__name__)

_FOLLOWUP_PROMPT = (
    "Based on the answer below, suggest 2-3 short, natural follow-up "
    "questions a user might ask next. Respond with ONLY the questions, "
    "one per line, no numbering, no extra commentary.\n\nAnswer:\n{answer}"
)


# --- Node: follow-up suggestions (GRAPH_PATTERNS.md pattern 27) ---
def make_suggest_followups_node(llm):
    """Factory, same rationale as make_agent_node: needs an LLM client.

    Only reached once a turn is confirmed final (route_after_check's
    non-retry branch), so it never runs on an answer about to be retried.
    """

    async def suggest_followups(state: State) -> dict:
        """Suggests follow-ups only for a GROUNDED answer (`used_citations`
        non-empty) — no citations means nothing to build from, which
        naturally suppresses this for a refusal, general-knowledge aside,
        or ask_clarification response without special-casing any of them.

        Skipped on a cache hit: would mean a fresh LLM call on what's
        supposed to be the zero-LLM-call fast path.

        Degrades to `{"followups": []}` on any failure — enrichment on an
        already-complete answer, never something that fails the turn.
        """
        if state.get("cache_hit"):
            return {"followups": []}
        used_citations = state.get("used_citations") or []
        if not used_citations:
            return {"followups": []}
        last = state["messages"][-1]
        content = getattr(last, "content", "") or ""
        if not content:
            return {"followups": []}
        try:
            response = await llm.ainvoke(
                [HumanMessage(content=_FOLLOWUP_PROMPT.format(answer=content))]
            )
            lines = [
                line.strip("-•* ").strip()
                for line in (response.content or "").split("\n")
                if line.strip()
            ]
            return {"followups": lines[:3]}
        except Exception as exc:  # noqa: BLE001 - enrichment, never fail the turn
            logger.warning(
                "follow-up suggestion failed; continuing without follow-ups",
                extra={"node": "suggest_followups", "error_class": type(exc).__name__},
            )
            return {"followups": []}

    return suggest_followups
