"""`route_model`: the one node that may ask for a stronger model, and the rule for when asking failed (specs/014, GRAPH_PATTERNS pattern 68).

WHAT THE ROUTER MAY DO. Write `State.model_route` ("local" or "strong") and nothing else. It cannot see or change which tools a turn may call,
whether a write needs approval, whose data is read, or whether a write is repeated: those are decided by `human_approval`, `SecurityCtx` and
`idempotent()`, and they run unchanged under either model. A router that could touch them would be a second authority over the
non-negotiable rules; this one is advice that selects a price and a capability level for the model's reply, and a test pins that the
tool-gating behaviour is identical under both routes.

WHERE IT RUNS. After moderation, the semantic cache and compaction, before retrieval (build.py). So it is never reached for a refused input,
a cache hit, or a resumed turn, and the cost of asking is only paid by a turn that is about to reach a model. It is a node of the parent graph
only: a nested subagent run has its own model and its own budgets (`SUBAGENT_PROFILE`).

WHAT IT SENDS. The user's current message, to the ML service, under a short deadline (`app/core/routing_advice.py`). Not the tenant, the
principal, the history or the retrieved text.

EVERY FAILURE IS "LOCAL". The classifier is down, slow, wrong-shaped, or has nothing to say: the turn runs on the model it would have run on
before this feature existed, and the outcome is counted so an operator can tell why. The one exception to "the router never makes a turn
worse" is the strong model itself failing mid-turn, which `agent()` handles by repeating that one call locally (`strong_unreachable` below; checked against a real LiteLLM, not assumed).

NOTHING HERE RAISES. A node that raised would turn an advisory feature into an outage.
"""
import logging
import time
from typing import Literal

import openai
from langchain_core.runnables import RunnableConfig

from app.agent.cost import pricing
from app.agent.graph.messages import human_text, last_human_message
from app.agent.graph.state import State
from app.core import gateway, metrics, routing_advice
from app.core.config import ROUTER_ENABLED, ROUTER_THRESHOLD, STRONG_CHAT_MODEL

logger = logging.getLogger(__name__)

Route = Literal["local", "strong"]

def strong_unreachable(exc: BaseException) -> bool:
    """True if `exc` means the strong alias never produced an answer: a connection failure, a timeout, or the gateway reporting a server error (5xx).

    WHY ANY 5xx AND NOT 502/503/504. The first version listed those three, from reading about proxies. A real LiteLLM in front of an unreachable
    upstream answers HTTP **500** (`litellm.APIConnectionError: ... Cannot connect to host`), and so does an upstream that is up but does not have
    the model, so that list would never have fired for the failure it exists for. The gateway does not distinguish "down" from "broken" by status,
    and for this decision it does not need to: either way the strong model did not answer, and repeating the one call on the local model is the
    behaviour the app had before routing existed. It is counted (`strong_failed_fell_back`) and alerted on, so a persistently broken alias is loud,
    not hidden.

    NOT the gateway's budget stop (`provider_budget_exceeded`): that refusal repeats until the budget resets and is the operator's signal, which a
    silent fallback would hide. NOT any 4xx: the strong model was reached and refused the request (a bad request, a bad key, a rate limit), which
    a retry on another model would paper over rather than fix. Checked in this order so a budget stop that arrives as a 5xx is still not caught."""
    if gateway.is_budget_exceeded(exc):
        return False
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


async def _decide(state: State) -> tuple[Route, str, routing_advice.Advice | None]:
    if not ROUTER_ENABLED or not STRONG_CHAT_MODEL:
        return "local", "disabled", None
    text = human_text(last_human_message(list(state.get("messages") or [])))
    if not text.strip():
        return "local", "no_advice", None
    # An unpriced strong alias under UNPRICED_MODEL_POLICY=block would make `agent()` refuse the turn; the router must never be the reason a
    # turn is refused, so the route stays local without asking the classifier.
    if await pricing.refuse_unpriced(STRONG_CHAT_MODEL):
        return "local", "strong_unpriced", None
    advice = await routing_advice.advise(text)
    if advice.outcome != "advice" or advice.score is None:
        return "local", advice.outcome, advice
    if advice.score >= ROUTER_THRESHOLD:
        return "strong", "strong", advice
    return "local", "local", advice


def make_route_model_node():
    async def route_model(state: State, config: RunnableConfig | None = None) -> dict:
        started = time.perf_counter()
        try:
            route, outcome, advice = await _decide(state)
        except Exception as exc:  # noqa: BLE001 - routing is advisory: whatever went wrong, the turn stays on the local model
            route, outcome, advice = "local", "classifier_error", None
            logger.warning("route_model failed", extra={"error_class": type(exc).__name__})
        elapsed = time.perf_counter() - started
        metrics.agent_model_route_total.labels(outcome=outcome).inc()
        if outcome != "disabled":  # a switched-off router adds nothing to a turn, and a histogram of zeros would hide the real figures
            metrics.agent_model_route_latency_seconds.observe(elapsed)
            score = advice.score if advice is not None else None
            logger.info(
                "model_route",
                extra={
                    "outcome": outcome,
                    "route": route,
                    "served_by": advice.served_by if advice is not None else "none",
                    "head_version": advice.head_version if advice is not None else None,
                    "score_decile": None if score is None else min(int(score * 10), 9),  # a band, never the score or the text
                    "latency_ms": round(elapsed * 1000, 1),
                    "run_id": state.get("run_id", "-"),
                },
            )
        return {"model_route": route}

    return route_model
