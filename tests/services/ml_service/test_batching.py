"""The micro-batcher moved verbatim out of the old main.py; these pin the behavior the service relies on."""
import asyncio

from ml_service.batching import MicroBatcher


async def test_concurrent_submits_from_different_callers_share_one_pass():
    calls: list[list[int]] = []

    def batch_fn(items: list[int]) -> list[int]:
        calls.append(list(items))
        return [i * 2 for i in items]

    batcher: MicroBatcher[int, int] = MicroBatcher(batch_fn, max_batch_size=16, max_wait_ms=100)
    batcher.start()
    try:
        results = await asyncio.gather(*(batcher.submit(i) for i in range(5)))
    finally:
        batcher.stop()

    assert results == [0, 2, 4, 6, 8]  # each caller gets its own result, in submit order
    assert calls == [[0, 1, 2, 3, 4]]  # one forward pass, not five


async def test_a_batch_never_exceeds_the_maximum_size():
    calls: list[int] = []

    def batch_fn(items: list[int]) -> list[int]:
        calls.append(len(items))
        return items

    batcher: MicroBatcher[int, int] = MicroBatcher(batch_fn, max_batch_size=2, max_wait_ms=100)
    batcher.start()
    try:
        await asyncio.gather(*(batcher.submit(i) for i in range(5)))
    finally:
        batcher.stop()

    assert max(calls) <= 2 and sum(calls) == 5


async def test_a_failed_batch_fails_every_waiter_in_it_and_the_next_batch_still_runs():
    state = {"calls": 0}

    def batch_fn(items: list[int]) -> list[int]:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("boom")
        return items

    batcher: MicroBatcher[int, int] = MicroBatcher(batch_fn, max_batch_size=16, max_wait_ms=100)
    batcher.start()
    try:
        first = await asyncio.gather(*(batcher.submit(i) for i in range(3)), return_exceptions=True)
        assert all(isinstance(r, RuntimeError) for r in first)  # nobody hangs on a failed batch
        assert await batcher.submit(7) == 7  # the loop survived
    finally:
        batcher.stop()


async def test_stop_cancels_the_loop():
    batcher: MicroBatcher[int, int] = MicroBatcher(lambda items: items, max_batch_size=4, max_wait_ms=1)
    batcher.start()
    task = batcher._task  # noqa: SLF001 - observing the loop's task is the point of this test
    assert task is not None and not task.done()
    batcher.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
