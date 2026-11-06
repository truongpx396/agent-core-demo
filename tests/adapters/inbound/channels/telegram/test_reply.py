"""The growing reply (app/adapters/inbound/channels/telegram/reply.py, specs/021 FR-005): paced edits, and a last word that always lands."""
from app.adapters.inbound.channels.telegram.api import MESSAGE_CHAR_LIMIT
from app.adapters.inbound.channels.telegram.reply import ReplySink
from tests.adapters.inbound.channels.telegram.fakes import FakeApi


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def sink(api, clock, **kw):
    return ReplySink(api, 1, interval=2.0, clock=clock, **kw)


class TestPacing:
    async def test_the_first_words_are_sent_at_once_and_later_ones_edit_that_message_no_faster_than_the_interval(self):
        api, clock = FakeApi(), Clock()
        s = sink(api, clock)

        await s.push("Hel")
        clock.now += 0.5
        await s.push("Hello")  # too soon
        clock.now += 2.0
        await s.push("Hello there")

        assert api.sent_texts() == ["Hel"]
        assert [c[3] for c in api.of("edit")] == ["Hello there"]

    async def test_the_same_text_is_not_sent_twice(self):
        api, clock = FakeApi(), Clock()
        s = sink(api, clock)
        await s.push("Hel")
        clock.now += 5

        await s.push("Hel")

        assert api.of("edit") == []

    async def test_blank_text_is_not_a_reply_yet(self):
        api = FakeApi()

        await sink(api, Clock()).push("  \n")

        assert api.calls == []

    async def test_a_reply_too_long_for_one_message_stops_growing_and_waits_for_the_end(self):
        api, clock = FakeApi(), Clock()
        s = sink(api, clock)
        await s.push("short")
        clock.now += 5

        await s.push("x" * (MESSAGE_CHAR_LIMIT + 1))

        assert api.of("edit") == []

    async def test_disabled_it_sends_nothing_until_the_end(self):
        api = FakeApi()
        s = sink(api, Clock(), enabled=False)

        await s.push("Hel")
        await s.finish("Hello")

        assert api.sent_texts() == ["Hello"] and api.of("edit") == []

    async def test_if_the_first_message_did_not_go_it_does_not_keep_trying_to_edit_one_that_does_not_exist(self):
        api, clock = FakeApi(send_fails=True), Clock()
        s = sink(api, clock)
        await s.push("Hel")
        clock.now += 5

        await s.push("Hello")

        assert api.of("edit") == [] and len(api.of("send")) == 1


class TestTheLastWord:
    async def test_it_replaces_the_growing_message_and_is_not_throttled(self):
        api, clock = FakeApi(), Clock()
        s = sink(api, clock)
        await s.push("Hel")

        await s.finish("Hello there")  # no time has passed

        assert api.of("edit")[-1][3] == "Hello there" and api.sent_texts() == ["Hel"]

    async def test_a_text_that_is_already_shown_is_not_edited_again(self):
        api = FakeApi()
        s = sink(api, Clock())
        await s.push("done")

        await s.finish("done")

        assert api.of("edit") == []

    async def test_a_long_text_edits_the_first_part_and_sends_the_rest(self):
        api = FakeApi()
        s = sink(api, Clock())
        await s.push("start")
        text = "a" * MESSAGE_CHAR_LIMIT + "b" * 10

        await s.finish(text)

        assert api.of("edit")[-1][3] == "a" * MESSAGE_CHAR_LIMIT
        assert api.sent_texts()[-1] == "b" * 10

    async def test_an_edit_telegram_refuses_falls_back_to_sending_the_text_as_a_new_message(self):
        api = FakeApi(edit_fails=True)
        s = sink(api, Clock())
        await s.push("Hel")

        await s.finish("Hello there")

        assert api.sent_texts() == ["Hel", "Hello there"]

    async def test_with_nothing_shown_before_it_sends_everything(self):
        api = FakeApi()

        await sink(api, Clock()).finish("a" * (MESSAGE_CHAR_LIMIT + 5))

        assert [len(t) for t in api.sent_texts()] == [MESSAGE_CHAR_LIMIT, 5]

    async def test_an_empty_text_sends_nothing(self):
        api = FakeApi()

        await sink(api, Clock()).finish("")

        assert api.calls == []
