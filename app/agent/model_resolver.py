"""Resolves a chat model ALIAS (`app.core.config.CHAT_MODEL`, e.g. `"chat"`) to
the concrete model LiteLLM actually routes it to (e.g.
`"ollama_chat/qwen2.5:3b"`) — via LiteLLM's own `GET /model/info` admin
endpoint (GRAPH_PATTERNS.md pattern 38, verified empirically: the alias
appears as `data[].model_name`, the resolved model as
`data[].litellm_params.model`; the OpenAI-compatible chat response body
and LangChain's own `response_metadata` both only ever echo back the
ALIAS — `x-litellm-model-name` on the raw HTTP response header carries
the resolved value per-call, but LangChain's `ChatOpenAI` client doesn't
surface response headers through `.invoke()`, so this queries the
resolution directly instead of trying to intercept a header LangChain
throws away).

Naming only aliases (as `app/core/config.py::CHAT_MODEL` already does) is what
keeps this app portable — swapping providers is a config change, not a
code change — but it also means the single input with the largest effect
on output quality can change (a gateway remap) without any artifact this
app records changing with it. Resolving and recording it here keeps model
choice invisible to *routing* (nothing downstream branches on it) while
leaving it visible to *forensics* (app/agent/meter.py's usage ledger).
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
    failure (LiteLLM unreachable, alias not found, unexpected response
    shape) — this is observability, never something that should be able
    to block or fail a turn, same degrade-don't-crash posture every other
    enrichment layer in this app already takes.
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
