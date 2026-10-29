"""What the application asks the LLM gateway about its models: the price list and which model an alias resolves to.

Both come from one admin endpoint (LiteLLM's `GET /model/info`; `app/adapters/litellm/model_catalog.py`). The application never names a
provider model: it uses the gateway's aliases (`chat`, `embed`) and asks here only for what it needs to price a call
(`app/agent/cost/pricing.py`), to record which model answered (the usage events) and to fingerprint the embedding model
(`app/retrieval/embedding_identity.py`).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.ports.slot import Slot

# How long a lookup that failed (gateway unreachable, bad response) or found no such alias is remembered, per alias. Long enough that an
# outage costs one timeout per interval instead of one per turn; short enough that a gateway that comes back is noticed within a minute.
# Successes are cached for the process's life. Shared by the catalog adapter's alias lookup and the price list's refresh backoff.
FAILED_LOOKUP_RETRY_SECONDS = 60.0


class ModelCatalog(Protocol):
    async def model_info(self) -> list[Mapping]:
        """The gateway's `/model/info` entries (the price list the app reads). Raises when the gateway cannot be asked."""
        ...

    async def resolve(self, alias: str) -> str | None:
        """The provider model behind a gateway alias, or None when it cannot be found. Never raises."""
        ...


model_catalog: Slot[ModelCatalog] = Slot("model_catalog", ModelCatalog)
