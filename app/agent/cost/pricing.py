"""What a model costs, and the arithmetic that turns token usage into dollars
(spec 008 A2, GRAPH_PATTERNS.md patterns 26 and 35).

Prices are READ from LiteLLM (`GET /model/info`), not kept in a table here. The
proxy already owns the alias -> concrete-model mapping (model_resolver.py), so it
is the one place that can say what an alias costs: its entry carries
`input_cost_per_token`, `output_cost_per_token` and `cache_read_input_token_cost`,
merged from LiteLLM's maintained cost map, with any `model_info` a deployment sets
in the LiteLLM config taking precedence (verified in LiteLLM's
`_get_proxy_model_info`). Swapping the model behind an alias therefore re-prices
it with no code change, and a self-hosted model the cost map has never heard of is
priced where the model is defined.

The table this replaced held two entries and one blended rate per total token, so
any other alias cost $0 with no signal — and every dollar ceiling (per turn, per
subagent run, per tenant per day) multiplies tokens by a price, so they all went
blind together the moment `chat` pointed at a paid model.

Three rules keep a ceiling from under-counting:
  * Unknown is not free. A model LiteLLM reports no price for is `None` here, and
    only a price LiteLLM states as 0 (Ollama) is $0. `UNPRICED_MODEL_POLICY=block`
    refuses turns on an unpriced model; `allow` serves them but counts every call
    (`agent_unpriced_usage_total`, with an alert).
  * An alias served by several deployments is priced at its MOST EXPENSIVE one, and
    as unknown if any deployment is unpriced — the ceiling must bound the worst
    case, not the average.
  * Cached input tokens get the cache-read rate only when LiteLLM states one;
    otherwise they are billed at the full input rate.

Disclosed limits: a LiteLLM fallback (`chat` -> `chat-backup`) can serve a call at
another deployment's price and the OpenAI-compatible response does not say so
(the per-call `x-litellm-response-cost` header is not surfaced by `ChatOpenAI`);
and cache WRITE tokens, which some providers bill above the input rate, are billed
at the input rate because `langchain-openai` drops that field from `usage_metadata`.
"""
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.agent.model_resolver import admin_base_url
from app.core import metrics
from app.core.config import (
    OPENAI_API_KEY,
    PRICING_REFRESH_SECONDS,
    UNPRICED_MODEL_POLICY,
)

logger = logging.getLogger(__name__)

# After a failed read of /model/info the proxy is not asked again for this long
# (the last good prices keep serving meanwhile) — same reason as
# model_resolver.FAILED_LOOKUP_RETRY_SECONDS: an outage costs one timeout per
# interval, not one per LLM call.
FAILED_LOOKUP_RETRY_SECONDS = 60.0


@dataclass(frozen=True)
class ModelPrice:
    """USD per single token (LiteLLM's unit), not per 1K."""

    input_per_token: float
    output_per_token: float
    cache_read_per_token: float | None = None  # None: bill cached tokens at the input rate


# alias -> price, or None for an alias LiteLLM knows but cannot price. One read of
# /model/info covers every alias, so this is refreshed as a whole.
_prices: dict[str, ModelPrice | None] = {}
_fetched_at: float | None = None
_failed_at: float | None = None
_warned_unpriced: set[str] = set()


def reset_pricing_state() -> None:
    """Forgets every price and warning. For tests: this module's state is
    process-wide, so one test's prices must not leak into the next."""
    global _prices, _fetched_at, _failed_at
    _prices = {}
    _fetched_at = None
    _failed_at = None
    _warned_unpriced.clear()


def _rate(value: object) -> float | None:
    """A usable per-token rate: a finite, non-negative number. `bool` is an
    `int` in Python, so it is excluded explicitly."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _deployment_price(entry: Mapping) -> ModelPrice | None:
    info = entry.get("model_info") or {}
    input_rate = _rate(info.get("input_cost_per_token"))
    output_rate = _rate(info.get("output_cost_per_token"))
    if input_rate is None or output_rate is None:
        return None  # half a price is not a price
    return ModelPrice(input_rate, output_rate, _rate(info.get("cache_read_input_token_cost")))


def _alias_price(entries: list[Mapping]) -> ModelPrice | None:
    prices = [_deployment_price(entry) for entry in entries]
    if any(price is None for price in prices):
        return None
    known = [price for price in prices if price is not None]
    cache_rates = [price.cache_read_per_token for price in known]
    return ModelPrice(
        input_per_token=max(price.input_per_token for price in known),
        output_per_token=max(price.output_per_token for price in known),
        cache_read_per_token=(
            None
            if any(rate is None for rate in cache_rates)
            else max(rate for rate in cache_rates if rate is not None)
        ),
    )


def parse_model_info(entries: list[Mapping]) -> dict[str, ModelPrice | None]:
    """`/model/info`'s `data` list -> alias -> price. Entries sharing a
    `model_name` are deployments of one alias (see the module docstring)."""
    by_alias: dict[str, list[Mapping]] = {}
    for entry in entries:
        alias = entry.get("model_name")
        if isinstance(alias, str):
            by_alias.setdefault(alias, []).append(entry)
    return {alias: _alias_price(group) for alias, group in by_alias.items()}


async def _fetch_model_info() -> list[Mapping]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(
            f"{admin_base_url()}/model/info",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        )
    response.raise_for_status()
    return response.json().get("data", [])


async def get_price(alias: str) -> ModelPrice | None:
    """The alias's price, or None if it is unknown (not in LiteLLM, or LiteLLM
    cannot price it, or the proxy has never answered).

    Fail-soft in the direction that keeps ceilings working: when a refresh fails
    the LAST GOOD prices keep serving, so a proxy blip does not blind the
    ceilings; only a process that has never reached the proxy sees `None`, and
    `UNPRICED_MODEL_POLICY=block` is what turns that into a refused turn instead
    of unmetered spend. Concurrent callers may each refresh once at expiry — the
    read is idempotent, and a lock here would have to survive being used from
    several event loops (workers, tests)."""
    global _prices, _fetched_at, _failed_at
    now = time.monotonic()
    stale = _fetched_at is None or now - _fetched_at >= PRICING_REFRESH_SECONDS
    backing_off = _failed_at is not None and now - _failed_at < FAILED_LOOKUP_RETRY_SECONDS
    if stale and not backing_off:
        try:
            _prices = parse_model_info(await _fetch_model_info())
            _fetched_at = time.monotonic()
            _failed_at = None
        except Exception as exc:  # noqa: BLE001 - a failed price read must not fail the turn; last good prices keep serving
            _failed_at = time.monotonic()
            metrics.agent_cost_governance_degraded_total.labels(path="price_lookup").inc()
            logger.warning(
                "model price lookup failed; using the last known prices",
                extra={"error_class": type(exc).__name__},
            )
    return _prices.get(alias)


def cost_usd(
    price: ModelPrice, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> float:
    """Dollars for one call. `input_tokens` INCLUDES the cached prefix (the
    OpenAI-style `prompt_tokens` that `usage_metadata` carries), so the cached
    share is carved out of it and billed at the cache-read rate; a cached count
    larger than the input (a malformed report) is clamped rather than made negative."""
    input_tokens = max(input_tokens, 0)
    cached = min(max(cached_input_tokens, 0), input_tokens)
    cache_rate = (
        price.cache_read_per_token if price.cache_read_per_token is not None else price.input_per_token
    )
    return (
        (input_tokens - cached) * price.input_per_token
        + cached * cache_rate
        + max(output_tokens, 0) * price.output_per_token
    )


def note_unpriced(alias: str) -> None:
    """Counts one call made on a model with no price; warns the first time per alias."""
    metrics.agent_unpriced_usage_total.labels(model_alias=alias).inc()
    if alias not in _warned_unpriced:
        _warned_unpriced.add(alias)
        logger.warning(
            "model has no known price; dollar ceilings cannot see its spend",
            extra={"model_alias": alias},
        )


@dataclass(frozen=True)
class PricedCall:
    """What one LLM call cost, with the evidence. `cost_usd` is None when the model has no
    price (unknown is not free: a usage event stores NULL, never 0), and `price` is the rate
    snapshot it was computed from, so a later price change cannot rewrite what a call cost."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    cost_usd: float | None
    price: ModelPrice | None

    @property
    def spent_tokens(self) -> bool:
        return self.total_tokens > 0


async def price_call(alias: str, usage: Mapping) -> PricedCall:
    """Prices one LLM call from its LangChain `usage_metadata`, keeping the token split and the
    price used. A call that spent nothing prices to `cost_usd=0.0`; one on an unpriced model
    prices to None and is counted once here (`note_unpriced`).

    A report that gives a total but not its input/output split is billed at the dearer of the
    two rates for the unexplained remainder — never the cheaper."""
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reported_total = int(usage.get("total_tokens") or 0)
    cached = int((usage.get("input_token_details") or {}).get("cache_read") or 0)
    total_tokens = max(input_tokens + output_tokens, reported_total)
    if total_tokens <= 0:
        return PricedCall(0, 0, 0, 0, 0.0, None)
    price = await get_price(alias)
    if price is None:
        note_unpriced(alias)
        return PricedCall(input_tokens, output_tokens, cached, total_tokens, None, None)
    unexplained = max(reported_total - input_tokens - output_tokens, 0)
    cost = cost_usd(price, input_tokens, output_tokens, cached) + unexplained * max(
        price.input_per_token, price.output_per_token
    )
    return PricedCall(input_tokens, output_tokens, cached, total_tokens, cost, price)


async def price_usage(alias: str, usage: Mapping) -> float:
    """Dollars for one LLM call; 0.0 (and `note_unpriced`) when the model has no price, 0.0 when
    nothing was spent. The running-total callers (the in-run ceiling, the per-turn ledger row)
    want a number, so unknown folds to 0.0 here; a usage event keeps the None (`price_call`)."""
    return (await price_call(alias, usage)).cost_usd or 0.0


async def refuse_unpriced(alias: str) -> bool:
    """True when `UNPRICED_MODEL_POLICY` is "block" and `alias` has no price."""
    return UNPRICED_MODEL_POLICY == "block" and await get_price(alias) is None
