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
(usage_ledger.py).
"""
import logging

import httpx

from app.core.config import OPENAI_API_BASE, OPENAI_API_KEY

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}


def _admin_base_url() -> str:
    """LiteLLM's admin endpoints (GET /model/info) live at the proxy
    root, not under the OpenAI-compatible /v1 prefix `OPENAI_API_BASE`
    already points at."""
    return OPENAI_API_BASE.removesuffix("/v1").removesuffix("/")


def resolve_model(alias: str) -> str | None:
    """Best-effort, cached-per-process lookup. Returns `None` on any
    failure (LiteLLM unreachable, alias not found, bad response shape) —
    observability only, never something that blocks or fails a turn.
    """
    if alias in _cache:
        return _cache[alias]
    try:
        response = httpx.get(
            f"{_admin_base_url()}/model/info",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            timeout=5,
        )
        response.raise_for_status()
        for entry in response.json().get("data", []):
            if entry.get("model_name") == alias:
                resolved = entry.get("litellm_params", {}).get("model")
                if resolved:
                    _cache[alias] = resolved
                    return resolved
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "model resolution failed; continuing without it",
            extra={"alias": alias, "error_class": type(exc).__name__},
        )
        return None
