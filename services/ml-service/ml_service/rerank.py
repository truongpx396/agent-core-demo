"""POST /rerank: cross-encoder reranking (Xenova/ms-marco-MiniLM-L-6-v2 via fastembed's ONNX cross-encoder).

Moved from the old `main.py` with its wire contract untouched: `{query, texts, raw_scores}` -> `[{"index", "score", "text"}]`. It
keeps the EXACT contract TEI used, so switching ML_SERVICE_URL between TEI and this service was a zero-app-code-change swap when they
were compared head-to-head; this service won on every measured axis and is the only reranker this app runs.
`raw_scores` is accepted for wire compatibility only: the service always returns the raw, unbounded cross-encoder logit
(`MIN_RERANK_SCORE` in app/agent/tools/builtin.py is calibrated to that scale).
"""
import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ml_service.batching import MicroBatcher
from ml_service.http import unavailable
from ml_service.settings import Settings


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    raw_scores: bool = True  # accepted for wire-compatibility; this service only ever returns raw cross-encoder logits


class RerankService:
    """A loaded cross-encoder and its micro-batcher. Scores (query, text) pairs; concurrent callers' pairs share forward passes."""

    def __init__(self, model: Any, *, max_batch_size: int, max_wait_ms: float) -> None:
        self._model = model
        self._batcher: MicroBatcher[tuple[str, str], float] = MicroBatcher(
            self._score_pairs, max_batch_size=max_batch_size, max_wait_ms=max_wait_ms
        )

    def _score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [float(s) for s in self._model.rerank_pairs(pairs)]

    async def start(self) -> None:
        self._batcher.start()

    async def stop(self) -> None:
        self._batcher.stop()

    async def score(self, query: str, texts: list[str]) -> list[float]:
        return list(await asyncio.gather(*(self._batcher.submit((query, text)) for text in texts)))


def load_rerank_service(settings: Settings) -> RerankService:
    """Blocking: downloads (first run) and loads the model. Imported lazily so importing this module never pulls fastembed."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name=settings.rerank_model_name, threads=settings.onnx_intra_op_threads)
    return RerankService(
        model, max_batch_size=settings.rerank_max_batch_size, max_wait_ms=settings.max_batch_wait_ms
    )


router = APIRouter()


@router.post("/rerank", response_model=None)
async def rerank(req: RerankRequest, request: Request):
    service: RerankService | None = request.app.state.registry.ready_value("rerank")
    if service is None:
        return unavailable(request, "rerank")
    scores = await service.score(req.query, req.texts)
    return [
        {"index": i, "score": float(score), "text": text}
        for i, (score, text) in enumerate(zip(scores, req.texts, strict=True))
    ]
