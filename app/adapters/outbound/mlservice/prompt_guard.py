"""The `InjectionScorer` adapter (`app/ports/screening.py`): `POST /prompt-guard` on the `ml-service` container.

A fresh `httpx.AsyncClient` per call, not shared: a client shared across event loops (the worker has several) breaks on the loop that did not
create it, the same loop-affinity reasoning as the reranker. Adds about 25-100 ms per call; viable because `ml-service` is already local and
offline (no hosted API, no new model pull). Raises on any failure to reach it or to parse its answer.
"""
import httpx

from app.core.config import ML_SERVICE_URL

_ML_CHECK_TIMEOUT_SECONDS = 3


class MlServicePromptGuard:
    async def malicious_score(self, text: str) -> float:
        async with httpx.AsyncClient(timeout=_ML_CHECK_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{ML_SERVICE_URL}/prompt-guard", json={"texts": [text]})
        resp.raise_for_status()
        return resp.json()[0]["malicious_score"]
