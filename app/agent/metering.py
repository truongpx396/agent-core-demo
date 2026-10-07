"""The one place a chat-model call is made, priced and recorded.

Before this, `llm.ainvoke(...)` was called from five places and only one of them (the agent node)
priced its usage, so follow-up suggestions, history compaction and the cron scripts spent tokens that
no ledger, dollar ceiling or credit balance could see. A call site that must remember to meter is a
call site that will one day forget; routing them all through `metered_invoke` makes the meter the
default and its absence the thing a test can catch (tests/agent/test_metering_choke_point.py).

It does three things for a call, in order, so they cannot disagree about it:
  1. calls the model with the tenant's identity attached for the gateway (`gateway.py`);
  2. prices the reported usage once (`pricing.price_call`), keeping the token split and the rate;
  3. records one usage event (`usage_events.py`), best-effort and never raising.

It returns the response plus the figures the caller already needed (`usage`, and `cost_usd` with the
running-total convention that an unpriced call adds 0.0), so the agent node's per-turn ceiling, the
ledger row written from its running total, and the event all come from the same `PricedCall`.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.agent import gateway, pricing, usage_events


@dataclass(frozen=True)
class MeteredCall:
    response: Any
    usage: Mapping
    cost_usd: float
    priced: pricing.PricedCall


async def metered_invoke(llm, messages, *, config, kind: str, model_alias: str) -> MeteredCall:
    """`config` is the LangGraph node config (None when a test calls a node directly, in which
    case no identity is sent and no event is written: there is no tenant to meter to)."""
    response = await llm.ainvoke(messages, **gateway.identity_from_config(config))
    usage = getattr(response, "usage_metadata", None) or {}
    priced = await pricing.price_call(model_alias, usage)
    configurable = (config or {}).get("configurable") or {}
    await usage_events.record_call(
        configurable.get("ctx"),
        thread_id=configurable.get("thread_id") or "unknown",
        message_id=getattr(response, "id", None),
        kind=kind,
        model_alias=model_alias,
        priced=priced,
    )
    return MeteredCall(response=response, usage=usage, cost_usd=priced.cost_usd or 0.0, priced=priced)
