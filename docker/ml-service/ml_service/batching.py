"""Generic async micro-batching queue: the engine behind every endpoint of this service.

Moved verbatim from the old single-file `main.py` (the class is now public as `MicroBatcher`). It coalesces concurrent `submit` calls
from DIFFERENT callers into fewer, larger ONNX forward passes instead of each paying full per-call overhead serially — the same
"adaptive batching" idea TEI/Triton/BentoML/Ray Serve all offer, scoped down to what a few small models on CPU need.

Why hand-rolled instead of a framework: TEI's own supported reranking architectures (CamemBERT, XLM-RoBERTa, GTE, ModernBERT) exclude
plain BERT/MiniLM outright regardless of weight format (verified against TEI's docs), and it has no notion of a plain classification
model like Prompt Guard. BentoML's adaptive batching only coalesces requests the framework itself dispatches over HTTP — verified in
its source (`_bentoml_sdk/method.py::APIMethod._local_call` bypasses the batch dispatcher for any in-process self-call), which would
force a wire-contract redesign. Ray Serve's `@serve.batch` works but pulls the whole Ray runtime, a real memory cost this deployment
does not want. Triton's dynamic batcher expects tensor-in/tensor-out, not raw text, so both text models would need a Python backend.
"""
import asyncio
import time
from collections.abc import Callable
from typing import Generic, TypeVar

Item = TypeVar("Item")
Result = TypeVar("Result")


class MicroBatcher(Generic[Item, Result]):
    """Coalesces concurrent `submit` calls into fewer batched calls to `batch_fn`, one batch at a time (via `asyncio.to_thread`, so
    batches never compete with each other for the same cores)."""

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
