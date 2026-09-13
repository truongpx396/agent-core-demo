"""Custom CPU inference microservice for this app's small, latency-critical
ONNX models. Currently two:
  - POST /rerank: cross-encoder reranking (Xenova/ms-marco-MiniLM-L-6-v2,
    via fastembed's ONNX cross-encoder) — app/retrieval/embeddings.py's
    `rerank`.
  - POST /prompt-guard: prompt-injection/jailbreak classification (Meta's
    Llama Prompt Guard 2, 22M, via gravitee-io's ONNX port) — the ML layer
    of app/agent/moderation.py's input screening.

Both share ONE hand-rolled async micro-batching queue design (_MicroBatcher
below) so concurrent calls from DIFFERENT callers get coalesced into fewer,
larger ONNX forward passes instead of each paying full per-call overhead
serially — the same "adaptive batching" idea TEI/Triton/BentoML/Ray Serve
all offer, scoped down to exactly what a couple of small models on CPU
need. This service was originally built (and proven, see docker-compose.yml's
`ml-service` comment for the numbers) for /rerank alone; /prompt-guard
reuses the SAME container and batching engine rather than standing up a
third service, since the serving problem — CPU, low memory, high
concurrency, high batching — is identical between the two; only the model
and its input/output shape differ.

Why hand-rolled instead of a framework: TEI's own supported reranking
architectures (CamemBERT, XLM-RoBERTa, GTE, ModernBERT) exclude plain
BERT/MiniLM outright regardless of weight format (verified directly
against TEI's own docs) — that's what motivated bge-reranker-base on TEI
in the first place, and why MiniLM needed a different server at all; TEI
also has no notion of a plain classification model like Prompt Guard.
BentoML's adaptive batching only coalesces requests the framework itself
dispatches over HTTP — verified directly in its source
(_bentoml_sdk/method.py::APIMethod._local_call bypasses the batch
dispatcher entirely for any in-process self-call), which would force a
wire-contract redesign (one HTTP call per item, not one call per request)
just to get real cross-request batching. Ray Serve's @serve.batch
genuinely works for this but pulls in the whole Ray runtime, a real
memory cost this deployment doesn't want. Triton's dynamic batcher is
transparent and mature but expects tensor-in/tensor-out, not raw text —
both models here need tokenization in front of it, meaning a Python
backend/ensemble config for what's otherwise a couple of small models.

/rerank keeps the EXACT wire contract TEI used ({query, texts, raw_scores}
-> [{"index", "score", "text"}]) so switching ML_SERVICE_URL
(app/core/config.py) between TEI and this service was a zero-app-code-change
swap when they were compared head-to-head — this service won on every
measured axis and is now the only reranker this app runs.

Both models load lazily and cache under $HOME/.cache/huggingface
(docker-compose.yml's `ml-service` volume) — same "pull once, then local"
shape as this app's other locally-run models (fastembed's sparse embedder,
the Ollama models). Neither pulls in `transformers`/`optimum`/`torch`:
verified directly that `optimum[onnxruntime]`'s own
ORTModelForSequenceClassification (the "standard" way to run a HF
sequence-classification ONNX model) drags in torch — 2GB+ — as a hard
transitive dependency purely for tensor conversion, even though the actual
compute already runs through ONNX Runtime either way. Both models here
need nothing that adds: fastembed's TextCrossEncoder already handles
/rerank's model+tokenizer directly, and Prompt Guard's tokenizer.json is a
complete standalone fast-tokenizer pipeline whose ONNX graph takes plain
input_ids/attention_mask int64 arrays and returns `logits` directly
(verified via `InferenceSession.get_inputs()/get_outputs()` against the
real downloaded model) — raw `onnxruntime` + `tokenizers` is enough for
both, same low-dependency-weight posture this whole service exists for.
"""
import asyncio
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI
from pydantic import BaseModel
from tokenizers import Tokenizer

RERANK_MODEL_NAME = os.environ.get("RERANK_MODEL_NAME", "Xenova/ms-marco-MiniLM-L-6-v2")

# gravitee-io's ONNX port of meta-llama/Llama-Prompt-Guard-2-22M — verified
# directly (huggingface_hub.snapshot_download, unauthenticated) that this
# specific repo is NOT gated, unlike the original meta-llama/ repo it's
# converted from (Llama 4 license, requires accepting terms + an HF token)
# — a real, practical blocker for an anonymous Docker build this port
# avoids. 22M, not the 86M sibling: Meta's own guidance is that
# "developers in resource constrained environments... will likely prefer
# the 22M model despite a slightly lower attack-prevention rate" — the
# same CPU/low-memory tradeoff this whole service already optimizes for.
# Override to gravitee-io/Llama-Prompt-Guard-2-86M-onnx for higher
# accuracy at more latency/memory cost if that tradeoff is ever wanted.
GUARD_MODEL_REPO = os.environ.get("GUARD_MODEL_REPO", "gravitee-io/Llama-Prompt-Guard-2-22M-onnx")
GUARD_MODEL_FILE = "model.quant.onnx"
GUARD_MAX_SEQ_LEN = 512  # this model's own max_position_embeddings (config.json), verified directly
GUARD_LABELS = {0: "BENIGN", 1: "MALICIOUS"}  # this model's own config.json id2label, verified directly

# How many items — (query, text) pairs for /rerank, single texts for
# /prompt-guard — to fold into one ONNX forward pass at most, per model.
# A real /rerank call in this app carries up to HYBRID_PREFETCH_LIMIT (20)
# pairs already, so this is sized in items, not "concurrent requests", to
# stay meaningful across different caller shapes.
RERANK_MAX_BATCH_SIZE = int(os.environ.get("RERANK_MAX_BATCH_SIZE", "256"))
GUARD_MAX_BATCH_SIZE = int(os.environ.get("GUARD_MAX_BATCH_SIZE", "256"))

# The adaptive-batching latency tax: the first item to land on an empty
# queue still waits up to this long for siblings before the batch is
# dispatched, trading a small fixed latency floor for coalescing
# opportunity (BentoML's own docs flag this exact trade-off). Kept small
# since both models are fast even at a real batch size — an 8ms admission
# window is proportionate, not dominating. Shared across both models'
# batchers; split into RERANK_/GUARD_-specific env vars if the two ever
# need different tuning.
MAX_BATCH_WAIT_MS = float(os.environ.get("MAX_BATCH_WAIT_MS", "8"))

# Each _MicroBatcher's loop dispatches batches one at a time — there is
# never more than one ONNX forward pass PER MODEL running at once, so it
# is safe (and the whole point) to give each pass every core rather than
# reserving threads=1 per process the way TEI/N-worker designs do to
# avoid oversubscription across CONCURRENT calls to the SAME model. The
# two models' batchers DO run concurrently with EACH OTHER (independent
# asyncio tasks, each with their own single-flight ONNX session) — real,
# accepted oversubscription risk between the two, not eliminated, since
# both are small enough that two full-core passes occasionally
# overlapping is far cheaper than serializing them through one shared
# queue.
ONNX_INTRA_OP_THREADS = int(os.environ.get("ONNX_INTRA_OP_THREADS", str(os.cpu_count() or 4)))

Item = TypeVar("Item")
Result = TypeVar("Result")


class _MicroBatcher(Generic[Item, Result]):
    """Generic async micro-batching queue: coalesces concurrent `submit`
    calls into fewer batched calls to `batch_fn`, one batch at a time (via
    asyncio.to_thread, so batches never compete with each other for the
    same cores) — the shared engine behind both /rerank and /prompt-guard,
    see this module's own docstring for why it's hand-rolled rather than a
    serving framework.
    """

    def __init__(
        self,
        batch_fn: Callable[[list[Item]], list[Result]],
        *,
        max_batch_size: int,
        max_wait_ms: float,
    ) -> None:
        self._batch_fn = batch_fn
        self._max_batch_size = max_batch_size
        self._max_wait_s = max_wait_ms / 1000
        self._queue: asyncio.Queue[tuple[Item, asyncio.Future[Result]]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def submit(self, item: Item) -> Result:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Result] = loop.create_future()
        await self._queue.put((item, fut))
        return await fut

    async def _loop(self) -> None:
        while True:
            first_item, first_fut = await self._queue.get()
            batch = [(first_item, first_fut)]
            deadline = time.monotonic() + self._max_wait_s
            while len(batch) < self._max_batch_size:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                try:
                    entry = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except TimeoutError:
                    break
                batch.append(entry)

            inputs = [item for item, _ in batch]
            try:
                results = await asyncio.to_thread(self._batch_fn, inputs)
            except Exception as exc:  # noqa: BLE001 - one failed batch must not crash the loop or hang every waiter in it
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(exc)
                continue
            for (_, fut), result in zip(batch, results, strict=True):
                if not fut.done():
                    fut.set_result(result)


_rerank_model = None
_rerank_batcher: "_MicroBatcher[tuple[str, str], float] | None" = None

_guard_session: ort.InferenceSession | None = None
_guard_tokenizer: Tokenizer | None = None
_guard_batcher: "_MicroBatcher[str, dict] | None" = None


def _load_rerank_model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=RERANK_MODEL_NAME, threads=ONNX_INTRA_OP_THREADS)


def _score_rerank_pairs(pairs: list[tuple[str, str]]) -> list[float]:
    return list(_rerank_model.rerank_pairs(pairs))


def _load_guard_model() -> tuple[ort.InferenceSession, Tokenizer]:
    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download(GUARD_MODEL_REPO, GUARD_MODEL_FILE)
    tokenizer_path = hf_hub_download(GUARD_MODEL_REPO, "tokenizer.json")

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = ONNX_INTRA_OP_THREADS
    session = ort.InferenceSession(model_path, sess_options=session_options)

    tokenizer = Tokenizer.from_file(tokenizer_path)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    tokenizer.enable_truncation(max_length=GUARD_MAX_SEQ_LEN)
    return session, tokenizer


def _score_guard_texts(texts: list[str]) -> list[dict]:
    assert _guard_tokenizer is not None and _guard_session is not None
    encoded = _guard_tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    (logits,) = _guard_session.run(
        None, {"input_ids": input_ids, "attention_mask": attention_mask}
    )
    exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
    probs = exp / exp.sum(axis=-1, keepdims=True)
    return [
        {"label": GUARD_LABELS[int(p.argmax())], "malicious_score": float(p[1])} for p in probs
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rerank_model, _rerank_batcher, _guard_session, _guard_tokenizer, _guard_batcher

    _rerank_model = await asyncio.to_thread(_load_rerank_model)
    _rerank_batcher = _MicroBatcher(
        _score_rerank_pairs, max_batch_size=RERANK_MAX_BATCH_SIZE, max_wait_ms=MAX_BATCH_WAIT_MS
    )
    _rerank_batcher.start()

    _guard_session, _guard_tokenizer = await asyncio.to_thread(_load_guard_model)
    _guard_batcher = _MicroBatcher(
        _score_guard_texts, max_batch_size=GUARD_MAX_BATCH_SIZE, max_wait_ms=MAX_BATCH_WAIT_MS
    )
    _guard_batcher.start()

    yield

    _rerank_batcher.stop()
    _guard_batcher.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    raw_scores: bool = True  # accepted for wire-compatibility; this service only ever returns raw cross-encoder logits


@app.post("/rerank")
async def rerank(req: RerankRequest) -> list[dict]:
    assert _rerank_batcher is not None
    scores = await asyncio.gather(
        *(_rerank_batcher.submit((req.query, text)) for text in req.texts)
    )
    return [
        {"index": i, "score": float(score), "text": text}
        for i, (score, text) in enumerate(zip(scores, req.texts, strict=True))
    ]


class PromptGuardRequest(BaseModel):
    texts: list[str]


@app.post("/prompt-guard")
async def prompt_guard(req: PromptGuardRequest) -> list[dict]:
    assert _guard_batcher is not None
    results = await asyncio.gather(*(_guard_batcher.submit(text) for text in req.texts))
    return [
        {"index": i, "label": r["label"], "malicious_score": r["malicious_score"], "text": text}
        for i, (r, text) in enumerate(zip(results, req.texts, strict=True))
    ]
