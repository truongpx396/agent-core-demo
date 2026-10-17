"""POST /prompt-guard: prompt-injection/jailbreak classification (Meta's Llama Prompt Guard 2, 22M, via gravitee-io's ONNX port).

The ML layer of app/core/moderation.py's input screening. Wire contract unchanged: `{texts}` -> `[{"index", "label",
"malicious_score", "text"}]`. Raw `onnxruntime` + `tokenizers`, deliberately not `transformers`/`optimum`: verified that
`optimum[onnxruntime]`'s ORTModelForSequenceClassification drags in torch (2 GB+) as a hard transitive dependency purely for tensor
conversion, although the compute already runs through ONNX Runtime either way. The graph takes plain input_ids/attention_mask int64
arrays and returns `logits` directly (verified via `InferenceSession.get_inputs()/get_outputs()` against the real model).
"""
import asyncio

import numpy as np
from fastapi import APIRouter, Request
from pydantic import BaseModel

from ml_service.batching import MicroBatcher
from ml_service.http import unavailable
from ml_service.onnx_text import OnnxText
from ml_service.settings import (
    GUARD_LABELS,
    GUARD_MAX_SEQ_LEN,
    GUARD_MODEL_FILE,
    Settings,
)


class PromptGuardRequest(BaseModel):
    texts: list[str]


def softmax(logits: np.ndarray) -> np.ndarray:
    exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return exp / exp.sum(axis=-1, keepdims=True)


class GuardService:
    def __init__(self, text: OnnxText, *, max_batch_size: int, max_wait_ms: float) -> None:
        self._text = text
        self._batcher: MicroBatcher[str, dict] = MicroBatcher(
            self._score_texts, max_batch_size=max_batch_size, max_wait_ms=max_wait_ms
        )

    def _score_texts(self, texts: list[str]) -> list[dict]:
        probs = softmax(self._text.logits(texts))
        return [{"label": GUARD_LABELS[int(p.argmax())], "malicious_score": float(p[1])} for p in probs]

    async def start(self) -> None:
        self._batcher.start()

    async def stop(self) -> None:
        self._batcher.stop()

    async def score(self, texts: list[str]) -> list[dict]:
        return list(await asyncio.gather(*(self._batcher.submit(t) for t in texts)))


def load_guard_service(settings: Settings) -> GuardService:
    """Blocking: downloads (first run) and loads the model. All heavy imports are deferred so importing this module is cheap."""
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    model_path = hf_hub_download(settings.guard_model_repo, GUARD_MODEL_FILE)
    tokenizer_path = hf_hub_download(settings.guard_model_repo, "tokenizer.json")
    options = ort.SessionOptions()
    options.intra_op_num_threads = settings.onnx_intra_op_threads
    session = ort.InferenceSession(model_path, sess_options=options)
    # pad_token "[PAD]" is looked up in the vocabulary, not assumed to be id 0 (it is 0 for this model; a test pins that).
    text = OnnxText(
        session,
        Tokenizer.from_file(tokenizer_path),
        pooling="none",
        normalize=False,
        max_length=GUARD_MAX_SEQ_LEN,
        pad_token="[PAD]",
    )
    return GuardService(text, max_batch_size=settings.guard_max_batch_size, max_wait_ms=settings.max_batch_wait_ms)


router = APIRouter()


@router.post("/prompt-guard", response_model=None)
async def prompt_guard(req: PromptGuardRequest, request: Request):
    service: GuardService | None = request.app.state.registry.ready_value("prompt-guard")
    if service is None:
        return unavailable(request, "prompt-guard")
    results = await service.score(req.texts)
    return [
        {"index": i, "label": r["label"], "malicious_score": r["malicious_score"], "text": text}
        for i, (r, text) in enumerate(zip(results, req.texts, strict=True))
    ]
