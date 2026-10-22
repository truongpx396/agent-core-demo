"""`route_model`: the one node that may ask for a different model, and the rule for when asking failed (specs/014, specs/015; GRAPH_PATTERNS pattern 68).

WHAT THE ROUTER MAY DO. Write `State.model_route` ("local", "medium" or "strong") and nothing else. It cannot see or change which tools a turn may
call, whether a write needs approval, whose data is read, or whether a write is repeated: those are decided by `human_approval`, `SecurityCtx` and
`idempotent()`, and they run unchanged under every tier. A router that could touch them would be a second authority over the non-negotiable rules;
this one is advice that selects a price and a capability level for the model's reply, and a test pins that the tool-gating behaviour is identical
under all three.

HOW IT DECIDES. Two signals each name the lowest tier they will accept (`app/core/routing_policy.py`, a pure module the evaluation harness runs too):
rules over the user's message, and the label the ML service's label set gives it (`app/core/classification.py`). The tier is the highest configured
tier at or below `min(ceiling, max(rules, label))`. A label that cannot be had (the service is down, slow, found no topic, or named one this app's
table does not know) leaves the rules to decide and is counted.

THREE MODES (`ROUTER_MODE`). `off` does nothing at all: no work, no call, byte-identical to a build without routing. `shadow` makes every decision, counts
it and logs it, and runs the turn on the local model, so an operator reads what the router WOULD have spent before it spends anything. `on` acts. The
decision in `shadow` is exactly the one `on` would make: there is one code path and the mode only picks what is written to `State`.

WHERE IT RUNS. After moderation, the semantic cache and compaction, before retrieval (build.py). So it is never reached for a refused input, a cache hit,
or a resumed turn, and the cost of asking is only paid by a turn that is about to reach a model. It is a node of the parent graph only: a nested subagent
run has its own model and its own budgets (`SUBAGENT_PROFILE`).

WHAT IT SENDS. The user's current message, to the ML service, under a short deadline. Not the tenant, the principal, the history or the retrieved text.

EVERY FAILURE LANDS ON A TIER THE OPERATOR ALREADY PAYS FOR. A label that cannot be had means the rules decide; an alias with no price under
`UNPRICED_MODEL_POLICY=block` (which would make `agent()` refuse the turn, and the router must never be the reason a turn is refused) steps down a tier;
anything unexpected is `local`. The one exception to "the router never makes a turn worse" is a routed model failing mid-turn, which `agent()` handles
by repeating that one call locally (`routed_unreachable` below; checked against a real LiteLLM, not assumed).

NOTHING HERE RAISES. A node that raised would turn an advisory feature into an outage.
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
    ROUTER_LABELSET,
    ROUTER_LONG_PROMPT_CHARS,
    ROUTER_MAX_TIER,
    ROUTER_MODE,
    STRONG_CHAT_MODEL,
)
from app.core.routing_policy import Tier

logger = logging.getLogger(__name__)

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
    """True if `exc` means a routed alias never produced an answer: a connection failure, a timeout, or the gateway reporting a server error (5xx).

    WHY ANY 5xx AND NOT 502/503/504. The first version listed those three, from reading about proxies. A real LiteLLM in front of an unreachable
    upstream answers HTTP **500** (`litellm.APIConnectionError: ... Cannot connect to host`), and so does an upstream that is up but does not have
    the model, so that list would never have fired for the failure it exists for. The gateway does not distinguish "down" from "broken" by status,
    and for this decision it does not need to: either way the routed model did not answer, and repeating the one call on the local model is the
    behaviour the app had before routing existed. It is counted (`routed_unreachable_fell_back`) and alerted on, so a persistently broken alias is loud,
    not hidden.

    NOT the gateway's budget stop (`provider_budget_exceeded`): that refusal repeats until the budget resets and is the operator's signal, which a
    silent fallback would hide. NOT any 4xx: the routed model was reached and refused the request (a bad request, a bad key, a rate limit), which
    a retry on another model would paper over rather than fix. Checked in this order so a budget stop that arrives as a 5xx is still not caught."""
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
    labelled = await classification.label(ROUTER_LABELSET, text) if ROUTER_LABELSET else None
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
