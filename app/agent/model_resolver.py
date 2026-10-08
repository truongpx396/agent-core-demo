"""Resolves a chat model ALIAS (`config.CHAT_MODEL`, e.g. "chat") to the
concrete model LiteLLM routes it to (e.g. "ollama_chat/qwen2.5:3b") via
LiteLLM's `GET /model/info` admin endpoint (pattern 38). The
OpenAI-compatible response body and LangChain's `response_metadata` only
ever echo back the alias — `x-litellm-model-name` carries the resolved
value per-call, but `ChatOpenAI.invoke()` doesn't surface response
headers, so this queries the resolution directly instead.

Naming only aliases keeps this app portable (swapping providers is a
config change), but means the biggest lever on output quality (a gateway
remap) can change without any recorded artifact reflecting it. Resolving
it here keeps model choice invisible to routing but visible to forensics
(the usage events, `usage_events.py`).

Async, with a short negative cache. The first version was a plain synchronous
`httpx.get` called from the per-turn ledger write (since retired), which ran on the event loop
at the end of every completed turn: a slow LiteLLM froze every other turn,
stream and health check on that worker for the length of the lookup (measured:
a 0.5 s answer stalled a concurrent heartbeat for 0.56 s), and while LiteLLM was
down EVERY recorded turn paid a fresh 5 s timeout for an answer that could not
have changed (spec 008, B19). Now the request is awaited, and a lookup that
failed, or found no such alias, is not repeated for `FAILED_LOOKUP_RETRY_SECONDS`.
"""
import logging
import time

import httpx

from app.core import metrics
from app.core.config import OPENAI_API_BASE, OPENAI_API_KEY

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}

# How long a lookup that failed (proxy unreachable, bad response) or found no
# such alias is remembered, per alias. Long enough that an outage costs one
# timeout per interval instead of one per turn; short enough that a proxy that
# comes back is noticed within a minute. Successes are cached for the process's
# life, as before.
FAILED_LOOKUP_RETRY_SECONDS = 60.0
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
