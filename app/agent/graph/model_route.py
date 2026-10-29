"""`route_model`: the one node that may ask for a different model, and what happens when asking fails (specs/014,
015; GRAPH_PATTERNS pattern 68).

What it may do: write `State.model_route` ("local", "medium" or "strong") and nothing else. It can't see or
change which tools a turn may call, whether a write needs approval, whose data is read or whether a write
repeats: `human_approval`, `SecurityCtx` and `idempotent()` decide those under every tier (a test pins
identical tool-gating under all three). A router that could touch them would be a second authority over the
non-negotiable rules.

How it decides: two signals each name the lowest tier they accept (`app/core/routing_policy.py`, pure, also
run by the evaluation harness): rules over the user's message, and the label from the ML service's label set
(`app/core/classification.py`). The tier is the highest configured tier at or below `min(ceiling, max(rules,
label))`. A label that can't be had (service down or slow, no topic, unknown topic) leaves the rules to
decide, and is counted.

`ROUTER_MODE`: `off` does nothing (byte-identical to a build without routing); `shadow` makes and logs every
decision but runs the turn locally, so an operator reads what the router WOULD have spent; `on` acts. One code
path: the mode only picks what is written to `State`.

It runs after moderation, the semantic cache and compaction, before retrieval (build.py), so only a turn about
to reach a model pays for asking; parent graph only (a subagent has its own model and budgets). It sends only
the user's current message, under a short deadline: no tenant, principal, history or retrieved text.

Every failure lands on a tier the operator already pays for. No label: the rules decide. An alias with no
price under `UNPRICED_MODEL_POLICY=block`: step down a tier (the router must never be why a turn is refused).
Anything unexpected: `local`. A routed model failing mid-turn is `agent()`'s to handle by repeating that call
locally (`routed_unreachable`). Nothing here raises: a raising node would turn an advisory feature into an
outage.
"""
import logging
import time
from dataclasses import dataclass, replace

import openai
from langchain_core.runnables import RunnableConfig

from app.agent.cost import pricing
from app.agent.graph.messages import human_text, last_human_message
from app.agent.graph.state import State
from app.core import classification, gateway, metrics, routing_policy
from app.core.config import (
    MEDIUM_CHAT_MODEL,
    ROUTER_LABELS,
    ROUTER_LONG_PROMPT_CHARS,
    ROUTER_MAX_TIER,
    ROUTER_MODE,
    STRONG_CHAT_MODEL,
)
from app.core.routing_policy import Tier

logger = logging.getLogger(__name__)

# The one question this node asks of the decision interface. Named after the ML service's label set; the options are exactly LABEL_TIERS' keys
# (tests/core/test_routing_policy.py), so a topic the app has no tier for cannot be offered, and one the set lacks is refused by the service.
ROUTE_QUESTION = classification.Choice("route-topics", routing_policy.ROUTE_INSTRUCTIONS, routing_policy.LABEL_DESCRIPTIONS)

Route = Tier


def configured_tiers() -> tuple[Tier, ...]:
    """The tiers that exist: `local` always (the app's own default model), the others only when the operator named an alias for them."""
    tiers: list[Tier] = ["local"]
    if MEDIUM_CHAT_MODEL:
        tiers.append("medium")
    if STRONG_CHAT_MODEL:
        tiers.append("strong")
    return tuple(tiers)


def tier_alias(route: object) -> str | None:
    """The gateway alias a routed turn calls instead of the default, or None for `local`, for no decision and for a tier whose alias is no longer set
    (the operator unset it between a pause and its resume: there is nothing to call, and the default model is what the turn had before routing)."""
    return {"medium": MEDIUM_CHAT_MODEL, "strong": STRONG_CHAT_MODEL}.get(str(route)) or None


def routed_unreachable(exc: BaseException) -> bool:
    """True if `exc` means a routed alias never produced an answer: a connection failure, a timeout, or the
    gateway reporting a server error (5xx).

    Any 5xx, not just 502/503/504: a real LiteLLM in front of an unreachable upstream answers HTTP **500**
    (`APIConnectionError: ... Cannot connect to host`), as does an upstream that is up but lacks the model, so
    the first version's 502/503/504 list never fired for the failure it exists for. The gateway doesn't
    distinguish "down" from "broken" by status and this decision doesn't need it: either way the routed model
    didn't answer, and repeating that one call locally is what the app did before routing. It is counted
    (`routed_unreachable_fell_back`) and alerted, so a persistently broken alias is loud.

    NOT the gateway's budget stop (`provider_budget_exceeded`): it repeats until the budget resets and is the
    operator's signal, which a silent fallback would hide. NOT any 4xx: the model was reached and refused (bad
    request, bad key, rate limit), which a retry elsewhere would paper over. Checked in that order, so a
    budget stop that arrives as a 5xx is still not caught.
    """
    if gateway.is_budget_exceeded(exc):
        return False
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


@dataclass(frozen=True)
class _Result:
    outcome: str
    decision: routing_policy.Decision | None = None
    labelled: classification.Labelled | None = None


async def _decide(state: State) -> _Result:
    text = human_text(last_human_message(list(state.get("messages") or [])))
    if not text.strip():
        return _Result("no_text")
    labelled = await classification.choose(ROUTE_QUESTION, text) if ROUTER_LABELS else None
    label = labelled.label if labelled is not None and labelled.outcome == "labelled" else None
    configured = configured_tiers()
    decision = routing_policy.decide(
        text, label, ceiling=ROUTER_MAX_TIER, configured=configured, long_prompt_chars=ROUTER_LONG_PROMPT_CHARS
    )
    outcome = "decided"
    if labelled is not None:
        if labelled.outcome != "labelled":
            outcome = {"no_answer": "label_none", "timeout": "label_timeout"}.get(labelled.outcome, "label_error")
        elif labelled.label is not None and labelled.label not in routing_policy.LABEL_TIERS:
            outcome = "label_unknown"  # a mounted set whose labels this app's table has not been told about: the rules decided
    tier = decision.tier
    while tier != "local" and await pricing.refuse_unpriced(tier_alias(tier) or ""):
        tier = routing_policy.step_down(tier, configured)
    if tier != decision.tier:
        decision = replace(decision, tier=tier, limited_by="unpriced")
        outcome = "tier_unpriced"
    return _Result(outcome, decision, labelled)


def make_route_model_node():
    async def route_model(state: State, config: RunnableConfig | None = None) -> dict:
        if ROUTER_MODE == "off":
            metrics.agent_model_route_total.labels(outcome="disabled").inc()  # a switched-off router adds nothing to a turn, so no latency and no log either
            return {"model_route": "local"}
        started = time.perf_counter()
        try:
            result = await _decide(state)
        except Exception as exc:  # noqa: BLE001 - routing is advisory: whatever went wrong, the turn stays on the local model
            result = _Result("error")
            logger.warning("route_model failed", extra={"error_class": type(exc).__name__})
        elapsed = time.perf_counter() - started
        decision, labelled = result.decision, result.labelled
        metrics.agent_model_route_total.labels(outcome=result.outcome).inc()
        metrics.agent_model_route_latency_seconds.observe(elapsed)
        if decision is not None:
            metrics.agent_model_route_tier_total.labels(tier=decision.tier, mode=ROUTER_MODE, decided_by=decision.decided_by).inc()
        similarity = labelled.similarity if labelled is not None else None
        logger.info(
            "model_route",
            extra={
                "outcome": result.outcome,
                "mode": ROUTER_MODE,
                "tier": decision.tier if decision is not None else "local",
                "asked": decision.asked if decision is not None else None,
                "decided_by": decision.decided_by if decision is not None else None,
                "limited_by": decision.limited_by if decision is not None else None,
                "rules": list(decision.rules) if decision is not None else [],
                "label": decision.label if decision is not None else None,
                "similarity_decile": None if similarity is None else min(max(int(similarity * 10), 0), 9),  # a band, never the value or the text
                "served_by": labelled.served_by if labelled is not None else "none",
                "labelset_version": labelled.labelset_version if labelled is not None else None,
                "latency_ms": round(elapsed * 1000, 1),
                "run_id": state.get("run_id", "-"),
            },
        )
        # `shadow` made the decision above and counted it; the turn it describes runs on the default model.
        return {"model_route": decision.tier if decision is not None and ROUTER_MODE == "on" else "local"}

    return route_model
