"""The Telegram inbox and its dispatcher (app/adapters/inbound/channels/telegram_inbox.py, specs/021 FR-006): updates are made durable before they are acknowledged, run in
parallel across chats and in order within one, survive a crash, and `/cancel` is never stuck behind the turn it is meant to stop."""
import asyncio
import json

import pytest

from app.adapters.inbound.channels.telegram_inbox import (
    MAX_ATTEMPTS,
    Dispatcher,
    Inbox,
    chat_of,
    is_cancel,
)
from app.core import metrics
from tests.adapters.inbound.channels.telegram_fakes import FakeRedis, message
from tests.conftest import metric_value as _count

INBOX, PROCESSING = "telegram:inbox:ecorp", "telegram:inbox:ecorp:processing"


def update(n, chat=1, text="hi"):
    return {"update_id": n, "message": message(text, chat_id=chat)}


async def eventually(condition, timeout=2.0):
    async def wait():
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


class TestHelpers:
    def test_the_chat_is_the_messages_or_the_chat_a_pressed_button_sits_in(self):
        assert chat_of({"message": {"chat": {"id": 5}}}) == 5
        assert chat_of({"callback_query": {"message": {"chat": {"id": 6}}}}) == 6
        assert chat_of({"edited_message": {}}) is None

    @pytest.mark.parametrize(("text", "expected"), [("/cancel", True), ("/cancel now", True), ("/cancel@my_bot", True), ("/cancelled", False), ("cancel", False), ("", False), (None, False)])
    def test_cancel_is_a_commands_first_word(self, text, expected):
        assert is_cancel({"message": {"text": text}}) is expected


class TestInbox:
    async def test_updates_come_out_in_the_order_they_went_in(self):
        inbox = Inbox(FakeRedis(), "ecorp")
        for n in (1, 2, 3):
            await inbox.push(update(n))

        taken = [(await inbox.take(1))[1]["update_id"] for _ in range(3)]

        assert taken == [1, 2, 3]
        assert await inbox.take(1) is None

    async def test_a_taken_update_stays_on_the_processing_list_until_it_is_done(self):
        redis = FakeRedis()
        inbox = Inbox(redis, "ecorp")
        await inbox.push(update(1))

        raw, _ = await inbox.take(1)

        assert redis.lists[PROCESSING] == [raw] and redis.lists[INBOX] == []
        await inbox.done(raw)
        assert redis.lists[PROCESSING] == []

    async def test_an_unreadable_entry_is_dropped_and_counted_and_does_not_block_the_one_behind_it(self):
        redis = FakeRedis()
        inbox = Inbox(redis, "ecorp")
        await inbox.push(update(2))
        redis.lists[INBOX].append("not json")  # the oldest, so the first to be taken
        before = _count(metrics.channel_telegram_update_total, outcome="dropped")

        _, taken = await inbox.take(1)

        assert taken["update_id"] == 2
        assert _count(metrics.channel_telegram_update_total, outcome="dropped") == before + 1
        assert "not json" not in redis.lists[PROCESSING]

    async def test_after_a_crash_what_was_in_flight_goes_back_to_the_front_with_an_attempt_counted(self):
        redis = FakeRedis()
        inbox = Inbox(redis, "ecorp")
        await inbox.push(update(1))
        await inbox.push(update(2))
        await inbox.take(1)  # update 1 is in flight when the process dies

        assert await inbox.requeue_unfinished() == 1

        again, _ = await inbox.take(1)
        assert json.loads(again) == {"u": update(1), "n": 1}  # first, ahead of update 2, and counted
        assert redis.lists[PROCESSING] == [again]

    async def test_an_update_that_has_crashed_the_process_too_often_is_dropped_not_retried_forever(self):
        redis = FakeRedis()
        inbox = Inbox(redis, "ecorp")
        redis.lists[PROCESSING] = [json.dumps({"u": update(1), "n": MAX_ATTEMPTS - 1})]
        before = _count(metrics.channel_telegram_update_total, outcome="dropped")

        assert await inbox.requeue_unfinished() == 0

        assert redis.lists.get(INBOX, []) == [] and redis.lists[PROCESSING] == []
        assert _count(metrics.channel_telegram_update_total, outcome="dropped") == before + 1

    async def test_releasing_an_update_that_never_started_puts_it_back_unchanged(self):
        redis = FakeRedis()
        inbox = Inbox(redis, "ecorp")
        await inbox.push(update(1))
        raw, _ = await inbox.take(1)

        await inbox.release(raw)

        assert redis.lists[PROCESSING] == [] and json.loads(redis.lists[INBOX][-1])["n"] == 0  # not a crash, so not an attempt


class Running:
    """Runs a dispatcher against an inbox until `stop()`; `events` records what the handler saw."""

    def __init__(self, handler, *, max_concurrent=2):
        self.redis = FakeRedis()
        self.inbox = Inbox(self.redis, "ecorp")
        self.stop_event = asyncio.Event()
        self.dispatcher = Dispatcher(self.inbox, handler, max_concurrent=max_concurrent)
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.dispatcher.run(self.stop_event))
        return self

    async def __aexit__(self, *exc):
        self.stop_event.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)


class TestDispatcher:
    async def test_two_chats_are_served_at_the_same_time_so_a_slow_one_delays_only_itself(self):
        gate, done = asyncio.Event(), []

        async def handler(u):
            if u["message"]["chat"]["id"] == 1:
                await gate.wait()
            done.append(u["update_id"])

        async with Running(handler) as run:
            await run.inbox.push(update(1, chat=1))
            await run.inbox.push(update(2, chat=2))
            await eventually(lambda: done == [2])  # chat 2 was answered while chat 1 is still stuck
            gate.set()
            await eventually(lambda: done == [2, 1])

    async def test_one_chats_updates_run_one_at_a_time_in_the_order_they_arrived(self):
        log = []

        async def handler(u):
            log.append(("start", u["update_id"]))
            await asyncio.sleep(0.02)
            log.append(("end", u["update_id"]))

        async with Running(handler, max_concurrent=4) as run:
            for n in (1, 2, 3):
                await run.inbox.push(update(n, chat=1))
            await eventually(lambda: len(log) == 6)

        assert log == [("start", 1), ("end", 1), ("start", 2), ("end", 2), ("start", 3), ("end", 3)]

    async def test_cancel_does_not_wait_behind_the_turn_it_is_meant_to_stop(self):
        gate, done = asyncio.Event(), []

        async def handler(u):
            if u["message"]["text"] != "/cancel":
                await gate.wait()
            done.append(u["message"]["text"])

        async with Running(handler) as run:
            await run.inbox.push(update(1, chat=1, text="a long question"))
            await run.inbox.push(update(2, chat=1, text="/cancel"))
            await eventually(lambda: done == ["/cancel"])
            gate.set()
            await eventually(lambda: done == ["/cancel", "a long question"])

    async def test_no_more_chats_run_at_once_than_the_limit(self):
        running, peak = 0, 0

        async def handler(u):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

        async with Running(handler, max_concurrent=2) as run:
            for chat in (1, 2, 3, 4):
                await run.inbox.push(update(chat, chat=chat))
            await eventually(lambda: not run.redis.lists.get(INBOX) and not run.redis.lists.get(PROCESSING) and running == 0 and peak > 0)

        assert peak == 2

    async def test_a_handler_that_fails_is_counted_and_dropped_and_the_next_update_still_runs(self):
        seen = []

        async def handler(u):
            seen.append(u["update_id"])
            if u["update_id"] == 1:
                raise RuntimeError("boom")

        before = _count(metrics.channel_telegram_update_total, outcome="error")
        async with Running(handler) as run:
            await run.inbox.push(update(1, chat=1))
            await run.inbox.push(update(2, chat=1))
            await eventually(lambda: seen == [1, 2] and not run.redis.lists[PROCESSING])

        assert _count(metrics.channel_telegram_update_total, outcome="error") == before + 1

    async def test_a_finished_update_leaves_the_processing_list(self):
        async def handler(u):
            return None

        async with Running(handler) as run:
            await run.inbox.push(update(1))
            await eventually(lambda: run.redis.lists.get(INBOX) == [] and run.redis.lists.get(PROCESSING) == [])

    async def test_whatever_was_in_flight_when_the_last_process_died_is_picked_up_first(self):
        seen = []

        async def handler(u):
            seen.append(u["update_id"])

        redis_first = FakeRedis()
        redis_first.lists[PROCESSING] = [json.dumps({"u": update(1), "n": 0})]
        run = Running(handler)
        run.redis, run.inbox = redis_first, Inbox(redis_first, "ecorp")
        run.dispatcher = Dispatcher(run.inbox, handler, max_concurrent=2)
        async with run:
            await eventually(lambda: seen == [1])

    async def test_updates_not_started_when_it_shuts_down_go_back_to_the_inbox_untouched(self):
        gate, seen = asyncio.Event(), []

        async def handler(u):
            seen.append(u["update_id"])
            await gate.wait()

        run = Running(handler, max_concurrent=1)
        async with run:
            await run.inbox.push(update(1, chat=1))
            await eventually(lambda: seen == [1])
            await run.inbox.push(update(2, chat=2))  # taken, but waiting for the only slot
            await eventually(lambda: run.redis.lists.get(INBOX) == [] and len(run.redis.lists[PROCESSING]) == 2)
            run.stop_event.set()
            await asyncio.sleep(0.05)
            gate.set()

        assert seen == [1]  # update 2 never started
        assert [json.loads(raw)["u"]["update_id"] for raw in run.redis.lists[INBOX]] == [2]
        assert json.loads(run.redis.lists[INBOX][0])["n"] == 0
