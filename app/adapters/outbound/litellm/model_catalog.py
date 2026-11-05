"""The `ModelCatalog` adapter (`app/ports/models.py`): what LiteLLM's `GET /model/info` admin endpoint says about the models behind the
gateway's aliases. It answers two questions for the application, the price list (`model_info`, read by `app/agent/cost/pricing.py`)
and the model an alias resolves to (`resolve`, read by the usage-event writer).

Resolve a chat model ALIAS (`config.CHAT_MODEL`, e.g. "chat") to the concrete model LiteLLM routes it to (e.g.
"ollama_chat/qwen2.5:3b") via LiteLLM's `GET /model/info` admin endpoint (pattern 38). The OpenAI-compatible
response body and LangChain's `response_metadata` echo only the alias; `x-litellm-model-name` carries the
resolved value per call, but `ChatOpenAI.invoke()` doesn't surface response headers, so this queries the
resolution directly.

Naming only aliases keeps the app portable (swapping providers is a config change) but lets the biggest lever
on output quality, a gateway remap, change with no recorded artifact. Resolving it here keeps model choice
invisible to routing but visible to forensics (the usage events, `app/agent/cost/usage_events.py`).

Async, with a short negative cache. The first version was a synchronous `httpx.get` on the event loop at the
end of every completed turn: a slow LiteLLM froze every other turn, stream and health check on that worker for
the length of the lookup (measured: a 0.5 s answer stalled a concurrent heartbeat for 0.56 s), and while
LiteLLM was down EVERY recorded turn paid a fresh 5 s timeout for an answer that couldn't have changed (spec
008, B19). Now the request is awaited, and a lookup that failed or found no such alias isn't repeated for
`FAILED_LOOKUP_RETRY_SECONDS`.
"""
import logging
import time
from collections.abc import Mapping

import httpx

from app.core import metrics
from app.core.config import OPENAI_API_BASE, OPENAI_API_KEY
from app.ports.models import FAILED_LOOKUP_RETRY_SECONDS

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}

_failed_at: dict[str, float] = {}


def admin_base_url() -> str:
    """LiteLLM's admin endpoints (GET /model/info) live at the proxy
    root, not under the OpenAI-compatible /v1 prefix `OPENAI_API_BASE`
    already points at."""
    return OPENAI_API_BASE.removesuffix("/v1").removesuffix("/")


async def resolve_model(alias: str) -> str | None:
    """Best-effort, cached-per-process lookup. Returns `None` on any
    failure (LiteLLM unreachable, alias not found, bad response shape) —
    observability only, never something that blocks or fails a turn. A failure
    is remembered for `FAILED_LOOKUP_RETRY_SECONDS` so a down proxy is asked
    about once per interval, not once per turn."""
    if alias in _cache:
        return _cache[alias]
    failed_at = _failed_at.get(alias)
    if failed_at is not None and time.monotonic() - failed_at < FAILED_LOOKUP_RETRY_SECONDS:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{admin_base_url()}/model/info",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            )
        response.raise_for_status()
        for entry in response.json().get("data", []):
            if entry.get("model_name") == alias:
                resolved = entry.get("litellm_params", {}).get("model")
                if resolved:
                    _cache[alias] = resolved
                    _failed_at.pop(alias, None)
                    return resolved
        _failed_at[alias] = time.monotonic()
        return None
    except Exception as exc:  # noqa: BLE001 - observability only, never fails a turn; counted instead
        _failed_at[alias] = time.monotonic()
        metrics.agent_cost_governance_degraded_total.labels(path="model_resolve").inc()
        logger.warning(
            "model resolution failed; continuing without it",
            extra={"alias": alias, "error_class": type(exc).__name__},
        )
        return None


async def model_info() -> list[Mapping]:
    """The gateway's `/model/info` entries. Raises on a transport or HTTP error: the caller (`pricing.get_price`) keeps serving the last good
    prices and counts the failure."""
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(
            f"{admin_base_url()}/model/info",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        )
    response.raise_for_status()
    return response.json().get("data", [])


class LiteLLMModelCatalog:
    """The adapter: the module's functions behind the `ModelCatalog` port. Each call looks the function up at CALL time, so a test that
    patches `model_catalog.resolve_model` or `model_catalog.model_info` is seen."""

    async def model_info(self) -> list[Mapping]:
        return await model_info()

    async def resolve(self, alias: str) -> str | None:
        return await resolve_model(alias)
