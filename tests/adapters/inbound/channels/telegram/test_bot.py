"""Tests for app/adapters/inbound/channels/telegram/bot.py — the bot's turn flow, its approval buttons, its commands and its start-up checks. Telegram's API, the door
(`app/adapters/inbound/channels/door.py`) and Redis are replaced by the fakes in tests/adapters/inbound/channels/telegram/fakes.py, so these never touch a network, a queue, a worker or
a model. What the door and the sign-in do with real HTTP is tested in test_door.py, test_oauth.py and test_people.py; the inbox, the webhook
server, linking and the Bot API client have their own files.
"""
import asyncio

import pytest

from app.adapters.inbound.channels.door import SIGNIN_MESSAGES
from app.adapters.inbound.channels.oauth import SignInRefusedError
from app.adapters.inbound.channels.telegram import bot as telegram_channel
from app.adapters.inbound.channels.telegram.approvals import (
    ApprovalStore,
    parse_callback,
)
from app.adapters.inbound.channels.telegram.bot import Bot
from app.adapters.inbound.channels.telegram.inbox import Inbox
from app.core import metrics
from app.core.config import settings
from tests.adapters.inbound.channels.telegram.fakes import (
    FakeApi,
    FakeChannel,
    FakeDoor,
    FakeRedis,
    message,
    pause,
)
from tests.conftest import metric_value as _count

THREAD = "telegram:p-42:1"  # person p-42 in chat 1, which is what `message()` defaults to


@pytest.fixture(autouse=True)
def _plain_replies(monkeypatch):
    """One message per reply keeps the assertions about WHAT is sent; streaming has its own tests."""
    monkeypatch.setattr(settings, "telegram_stream_replies", False)
    monkeypatch.setattr(settings, "telegram_approvals", True)


def make_bot(door=None, *, approvals=True, linker=None, refusal=None, api=None):
    door = door if door is not None else FakeDoor()
    api = api if api is not None else FakeApi()
    store = ApprovalStore(FakeRedis(), "ecorp", ttl_seconds=900) if approvals else None
    return Bot(api, FakeChannel(door, refusal=refusal), approvals=store, linker=linker), api, door, store


def tap(token, *, approve=True, user_id=42, chat_id=1, message_id=101, text="⚠️ This needs your approval"):
    return {
        "update_id": 9,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": text},
            "data": f"ap:{token}:{'y' if approve else 'n'}",
        },
    }


async def pending_token(bot, door, store, *, first_events=None):
    """Write one message that pauses, and return the token on its buttons."""
    door.scripts.insert(0, first_events or [pause("add_note")])
    await bot.handle_update({"update_id": 1, "message": message("save a note")})
    pending = await store.for_thread(THREAD)
    assert pending is not None
    return pending.token


class TestThreadId:
    def test_thread_id_is_stable_per_person_and_chat(self):
        assert telegram_channel._thread_id("p-1", 123) == telegram_channel._thread_id("p-1", 123)

    def test_thread_id_differs_across_chats(self):
        assert telegram_channel._thread_id("p-1", 1) != telegram_channel._thread_id("p-1", 2)

    def test_two_people_in_one_group_chat_get_their_own_conversations(self):
        assert telegram_channel._thread_id("p-1", 5) != telegram_channel._thread_id("p-2", 5)

    def test_it_names_its_owner_inside_the_reserved_namespace(self):
        """The API lets a caller claim a `telegram:` id only if it names them (app/adapters/inbound/api/routers/chat.py)."""
        assert telegram_channel._thread_id("abc-123", 7) == "telegram:abc-123:7"


class TestChannel:
    async def test_in_header_mode_the_user_is_named_in_headers_in_the_configured_tenant(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        channel = telegram_channel.Channel(object())

        door, principal = await channel.door_for(99)

        assert principal == "telegram:99"
        assert await door._auth.headers() == {"X-Tenant-Id": settings.default_tenant, "X-Principal-Id": "telegram:99"}
        assert door._domain == "support"

    async def test_in_identity_mode_each_user_is_the_person_they_sign_in_as(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        channel = telegram_channel.Channel(object())

        class _Person:
            def __init__(self, sub):
                self.sub = sub

            async def sign_in(self):
                return self.sub

        monkeypatch.setattr(channel.people, "person", lambda user_id: _Person(f"sub-{user_id}"))

        (_, one), (_, two) = await channel.door_for(1), await channel.door_for(2)

        assert (one, two) == ("sub-1", "sub-2")

    async def test_a_token_that_names_nobody_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        channel = telegram_channel.Channel(object())

        class _Nobody:
            async def sign_in(self):
                return ""

        monkeypatch.setattr(channel.people, "person", lambda user_id: _Nobody())

        with pytest.raises(SignInRefusedError) as exc:
            await channel.door_for(1)

        assert exc.value.reason == "credentials"


class TestFormatReply:
    def test_no_citations_returns_plain_text(self):
        assert telegram_channel._format_reply("hello there", []) == "hello there"

    def test_citations_appended_as_a_sources_footer(self):
        result = telegram_channel._format_reply("answer text", [{"marker": "[1]", "title": "Checkpointers", "doc_id": "abc"}])
        assert "answer text" in result and "Sources:" in result and "[1] Checkpointers" in result

    def test_falls_back_to_doc_id_when_title_missing(self):
        assert "abc123" in telegram_channel._format_reply("answer", [{"marker": "[1]", "doc_id": "abc123"}])


class TestATurn:
    async def test_it_goes_to_the_persons_thread_and_the_reply_comes_back(self):
        bot, api, door, _ = make_bot(FakeDoor([[{"type": "token", "content": "the answer"}]]))

        await bot.handle_update({"update_id": 1, "message": message("hi", chat_id=7, user_id=42)})

        (turn,) = door.turns
        assert (turn["text"], turn["thread_id"]) == ("hi", telegram_channel._thread_id("p-42", 7))
        assert api.sent_texts() == ["the answer"]

    async def test_an_update_id_becomes_a_replay_key_scoped_to_the_domain(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        bot, _, door, _ = make_bot()

        await bot.handle_update({"update_id": 777, "message": message()})

        assert door.turns[0]["replay_key"] == "telegram:support:update:777"

    async def test_the_same_update_id_on_another_domains_bot_is_another_submission(self, monkeypatch):
        """Two bots share a person's conversation for one chat and each numbers its updates on its own."""
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        support_key = telegram_channel._replay_key(5)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "sales")

        assert telegram_channel._replay_key(5) != support_key != telegram_channel._replay_key(6)

    async def test_without_an_update_id_there_is_no_replay_key(self):
        bot, _, door, _ = make_bot()

        await bot.handle_update({"message": message()})

        assert door.turns[0]["replay_key"] is None

    async def test_reply_includes_a_citations_footer(self):
        cited = [{"marker": "[1]", "title": "Refund Policy", "doc_id": "d1"}]
        bot, api, _, _ = make_bot(FakeDoor([[{"type": "token", "content": "grounded answer [1]"}, {"type": "citations", "items": cited}]]))

        await bot.handle_update({"update_id": 1, "message": message()})

        assert "Sources:" in api.sent_texts()[0]

    async def test_a_turn_that_produces_no_text_still_gets_an_explicit_reply_not_silence(self):
        bot, api, _, _ = make_bot(FakeDoor([[{"type": "done"}]]))

        await bot.handle_update({"update_id": 1, "message": message("save a note")})

        assert api.sent_texts() == [telegram_channel._NO_REPLY_FALLBACK]

    async def test_an_error_event_is_shown_as_the_reply_and_the_stream_it_abandoned_is_closed(self):
        closed = []

        class _Door(FakeDoor):
            async def stream_turn(self, text, thread_id, **kwargs):
                try:
                    yield {"type": "error", "content": "Sorry, that took too long."}
                    yield {"type": "done"}
                finally:
                    closed.append(True)

        bot, api, _, _ = make_bot(_Door())

        await bot.handle_update({"update_id": 1, "message": message()})

        assert api.sent_texts() == ["Sorry, that took too long."]
        assert closed == [True]


class TestAUserWhoCannotBeSignedIn:
    """Specs/020 FR-006 and FR-012: the person is told what is wrong, in words, and NOTHING is sent to the agent."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("unknown_person", "not registered"),
            ("forbidden", "not allowed"),
            ("credentials", "sign-in was refused"),
            ("unavailable", "unavailable"),
            ("not_linked", "/login"),
        ],
    )
    async def test_the_reply_says_why_and_no_turn_is_sent(self, reason, expected):
        bot, api, door, _ = make_bot(FakeDoor([[{"type": "token", "content": "must never be shown"}]]), refusal=reason)

        await bot.handle_update({"update_id": 1, "message": message(user_id=42)})

        (sent,) = api.sent_texts()
        assert expected in sent and "must never be shown" not in sent
        assert door.turns == []

    async def test_an_unregistered_user_is_given_the_id_an_administrator_needs(self):
        bot, api, _, _ = make_bot(refusal="unknown_person")

        await bot.handle_update({"update_id": 1, "message": message(user_id=424242)})

        assert "424242" in api.sent_texts()[0]

    def test_every_refusal_reason_has_words(self):
        assert {"forbidden", "unknown_person", "credentials", "unavailable", "not_linked"} <= set(SIGNIN_MESSAGES)


class TestOtherMessages:
    @pytest.mark.parametrize("media", ["photo", "voice", "document", "sticker"])
    async def test_media_is_answered_with_what_the_bot_can_read_not_silence(self, media):
        bot, api, door, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 42}, media: [{}]}})

        assert api.sent_texts() == [telegram_channel.NON_TEXT_REPLY]
        assert door.turns == []

    async def test_a_service_message_is_not_addressed_to_the_bot_and_gets_no_reply(self):
        bot, api, door, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 42}, "new_chat_members": [{}]}})

        assert (api.calls, door.turns) == ([], [])

    @pytest.mark.parametrize("text", ["/start", "/help", "/help@my_bot"])
    async def test_start_and_help_describe_the_bot_and_send_no_turn(self, text):
        bot, api, door, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": message(text)})

        assert api.sent_texts() == [telegram_channel.HELP]
        assert door.turns == []

    async def test_a_slash_word_the_bot_does_not_know_is_an_ordinary_question(self):
        bot, _, door, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": message("/what is the refund policy")})

        assert door.turns[0]["text"] == "/what is the refund policy"

    async def test_login_without_linking_set_up_says_so(self):
        bot, api, _, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": message("/login")})

        assert "not set up" in api.sent_texts()[0]

    async def test_login_in_a_group_is_refused_because_the_link_it_gives_is_one_persons(self):
        class _Linker:
            called = False

            async def login(self, *args):
                self.called = True

        linker = _Linker()
        bot, api, _, _ = make_bot(linker=linker)

        await bot.handle_update({"update_id": 1, "message": {**message("/login"), "chat": {"id": 1, "type": "supergroup"}}})

        assert "private chat" in api.sent_texts()[0] and not linker.called

    async def test_login_and_unlink_in_a_private_chat_reach_the_linker(self):
        seen = []

        class _Linker:
            async def login(self, chat_id, user_id, sender):
                seen.append(("login", chat_id, user_id, sender))

            async def unlink(self, chat_id, user_id):
                seen.append(("unlink", chat_id, user_id))

        bot, _, _, _ = make_bot(linker=_Linker())

        await bot.handle_update({"update_id": 1, "message": {**message("/login", chat_id=5, user_id=42), "from": {"id": 42, "first_name": "Ann"}}})
        await bot.handle_update({"update_id": 2, "message": message("/unlink", chat_id=5, user_id=42)})

        assert seen == [("login", 5, 42, {"id": 42, "first_name": "Ann"}), ("unlink", 5, 42)]


class TestAnApprovalIsAskedForWithButtons:
    async def test_a_turn_that_needs_approval_is_not_unattended_and_shows_what_it_wants_to_do(self):
        bot, api, door, store = make_bot(FakeDoor([[{"type": "token", "content": "Sure, saving that"}, pause("add_note")]]))
        before = _count(metrics.channel_telegram_approval_total, outcome="shown")

        await bot.handle_update({"update_id": 1, "message": message("save a note")})

        assert door.turns[0]["unattended"] is False
        texts = api.sent_texts()
        assert texts[0] == "Sure, saving that"
        assert "add_note" in texts[1] and "approval" in texts[1]
        buttons = api.of("send")[1][3]["inline_keyboard"][0]
        pending = await store.for_thread(THREAD)
        assert [parse_callback(b["callback_data"]) for b in buttons] == [(pending.token, True), (pending.token, False)]
        assert (pending.user_id, pending.chat_id, pending.message_id) == (42, 1, 102)
        assert _count(metrics.channel_telegram_approval_total, outcome="shown") == before + 1

    async def test_with_approvals_off_the_turn_is_unattended_and_a_pause_is_cancelled_not_asked(self, monkeypatch):
        """The worker declines a pause for an unattended turn, so the channel should never see one. It can: a rolling deploy that upgrades this
        channel before its workers leaves an old worker running the turn as an ordinary one, and the conversation would stay paused."""
        monkeypatch.setattr(settings, "telegram_approvals", False)
        bot, api, door, _ = make_bot(FakeDoor([[{"type": "token", "content": "Sure, saving that"}, pause("add_note", "add_note")]]), approvals=False)
        before = _count(metrics.agent_unattended_job_paused_total)

        await bot.handle_update({"update_id": 1, "message": message("save a note")})

        assert door.turns[0]["unattended"] is True
        assert door.cancels == [(THREAD, False)]
        assert _count(metrics.agent_unattended_job_paused_total) == before + 1
        (reply,) = api.sent_texts()
        assert "add_note" in reply and "person's approval" in reply and "wasn't done" in reply
        assert "Sure, saving that" not in reply  # a half-sentence promising the write is not the answer
        assert all(call[3] is None for call in api.of("send"))  # no buttons

    async def test_a_cancel_that_fails_still_gives_the_person_a_reply(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_approvals", False)
        bot, api, door, _ = make_bot(FakeDoor([[pause("add_note")]], cancel_fails=True), approvals=False)

        await bot.handle_update({"update_id": 1, "message": message()})

        assert len(door.cancels) == 1 and "person's approval" in api.sent_texts()[0]

    async def test_when_the_buttons_cannot_be_shown_the_pause_is_cancelled_so_nobody_is_stranded(self):
        """Nobody can answer a question that was never shown; leaving it paused would refuse every later message."""
        bot, _, door, store = make_bot(FakeDoor([[pause("add_note")]]), api=FakeApi(send_fails=True))

        await bot.handle_update({"update_id": 1, "message": message()})

        assert door.cancels == [(THREAD, False)]
        assert await store.for_thread(THREAD) is None

    async def test_a_redelivered_update_does_not_ask_twice(self):
        bot, api, door, _ = make_bot(FakeDoor([[pause("add_note")], [pause("add_note")]]))
        await bot.handle_update({"update_id": 1, "message": message("save a note")})
        asked = len(api.of("send"))

        await bot.handle_update({"update_id": 1, "message": message("save a note")})

        assert len(door.turns) == 1  # the reminder path never reached the door
        assert api.sent_texts()[asked:] == [telegram_channel._ASK_FIRST]


class TestTappingAButton:
    async def test_the_owner_approving_resumes_the_turn_once_and_the_reply_follows(self):
        bot, api, door, store = make_bot(FakeDoor(resumes=[[{"type": "token", "content": "Saved."}]]))
        token = await pending_token(bot, door, store)
        before = _count(metrics.channel_telegram_approval_total, outcome="approved")

        await bot.handle_update(tap(token, approve=True))

        assert door.resumes == [(THREAD, True)]
        assert api.sent_texts()[-1] == "Saved."
        assert any("Approved" in call[3] and call[4] == {"inline_keyboard": []} for call in api.of("edit"))  # decided, and no buttons left
        assert await store.for_thread(THREAD) is None
        assert _count(metrics.channel_telegram_approval_total, outcome="approved") == before + 1

    async def test_rejecting_resumes_with_a_no(self):
        bot, _, door, store = make_bot()
        token = await pending_token(bot, door, store)

        await bot.handle_update(tap(token, approve=False))

        assert door.resumes == [(THREAD, False)]

    async def test_someone_else_in_the_chat_pressing_the_button_does_nothing(self):
        """A group chat shows the buttons to everyone. Only the person the conversation belongs to may decide (Principle II)."""
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        before = _count(metrics.channel_telegram_approval_total, outcome="not_owner")

        await bot.handle_update(tap(token, user_id=7))

        assert door.resumes == []
        assert api.of("answer")[-1][3] is True  # an alert, so they see why
        assert _count(metrics.channel_telegram_approval_total, outcome="not_owner") == before + 1
        await bot.handle_update(tap(token, user_id=42))  # the refusal did not use the approval up
        assert door.resumes == [(THREAD, True)]

    async def test_a_second_tap_while_the_first_is_being_applied_does_nothing(self):
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        assert await store.claim(token)  # the first tap's claim

        await bot.handle_update(tap(token))

        assert door.resumes == []
        assert "Already being handled" in api.of("answer")[-1][2]

    async def test_a_tap_after_the_decision_was_applied_finds_nothing_to_approve(self):
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        await bot.handle_update(tap(token))

        await bot.handle_update(tap(token))

        assert door.resumes == [(THREAD, True)]  # once, not twice
        assert "expired or was already handled" in api.of("answer")[-1][2]

    async def test_a_tap_after_the_approval_expired_never_resumes_and_never_cancels(self):
        """Cancelling from a late tap would set a flag that stops a turn that is running by then; expiry only says so."""
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        for key in [k for k in store._redis.kv if k.startswith("telegram:approval:")]:
            del store._redis.kv[key]  # the record's TTL ran out
        before = _count(metrics.channel_telegram_approval_total, outcome="expired")

        await bot.handle_update(tap(token))

        assert door.resumes == [] and door.cancels == []
        assert _count(metrics.channel_telegram_approval_total, outcome="expired") == before + 1
        assert "No longer pending" in api.of("edit")[-1][3]

    async def test_an_approval_is_stored_with_the_configured_lifetime(self):
        bot, _, door, store = make_bot()
        await pending_token(bot, door, store)

        assert {ttl for key, ttl in store._redis.ttls.items() if key.startswith("telegram:approval:")} == {900}

    async def test_a_decision_the_door_refused_puts_the_buttons_back_and_can_be_made_again(self):
        refused = [{"type": "error", "content": "You are sending too much. Try again in 5 seconds."}]  # a door refusal carries no error code
        bot, api, door, store = make_bot(FakeDoor(resumes=[refused, [{"type": "token", "content": "Saved."}]]))
        token = await pending_token(bot, door, store)
        before = _count(metrics.channel_telegram_approval_total, outcome="resume_failed")

        await bot.handle_update(tap(token))

        assert _count(metrics.channel_telegram_approval_total, outcome="resume_failed") == before + 1
        assert api.of("edit")[-1][4]["inline_keyboard"][0]  # the buttons are back
        assert "tap again" in api.sent_texts()[-1]
        assert await store.for_thread(THREAD) is not None

        await bot.handle_update(tap(token))

        assert len(door.resumes) == 2 and api.sent_texts()[-1] == "Saved."

    async def test_a_decision_the_agent_could_not_apply_is_reported_and_not_offered_again(self):
        gone = [{"type": "error", "content": "checkpoint_lost: no paused run found", "code": "internal"}]
        bot, api, door, store = make_bot(FakeDoor(resumes=[gone]))
        token = await pending_token(bot, door, store)

        await bot.handle_update(tap(token))

        assert api.sent_texts()[-1].startswith("checkpoint_lost")
        assert await store.for_thread(THREAD) is None

    async def test_an_action_after_an_approval_that_needs_another_approval_asks_again_with_new_buttons(self):
        bot, api, door, store = make_bot(FakeDoor(resumes=[[{"type": "token", "content": "Done. Now sending."}, pause("send_email")]]))
        first = await pending_token(bot, door, store)

        await bot.handle_update(tap(first))

        second = await store.for_thread(THREAD)
        assert second is not None and second.token != first
        assert "send_email" in api.sent_texts()[-1]

    async def test_a_person_who_can_no_longer_be_signed_in_decides_nothing_and_is_told_once(self):
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        bot.channel.refusal = "forbidden"
        sent_before = len(api.sent_texts())

        await bot.handle_update(tap(token))

        assert door.resumes == []
        assert api.sent_texts()[sent_before:] == [SIGNIN_MESSAGES["forbidden"]]
        assert await store.claim(token)  # the claim was given back

    async def test_an_approval_asked_of_an_earlier_sign_in_is_not_applied_to_this_one(self):
        """The conversation id names its owner; if the person behind the Telegram id changed (a relink) the old question is not theirs."""
        bot, api, door, store = make_bot()
        token = await pending_token(bot, door, store)
        pending = await store.get(token)
        await store._save(type(pending)(**{**pending.__dict__, "thread_id": "telegram:someone-else:1"}))

        await bot.handle_update(tap(token))

        assert door.resumes == []
        assert "earlier sign-in" in api.sent_texts()[-1]

    @pytest.mark.parametrize("data", ["", "ap:", "ap:x:maybe", "other:x:y", None, 5])
    async def test_a_button_this_bot_did_not_make_is_not_active(self, data):
        bot, api, door, _ = make_bot()
        update = tap("x")
        update["callback_query"]["data"] = data

        await bot.handle_update(update)

        assert door.resumes == []
        assert "not active" in api.of("answer")[-1][2]

    async def test_a_tap_with_approvals_off_is_not_active(self):
        bot, api, door, _ = make_bot(approvals=False)

        await bot.handle_update(tap("anything"))

        assert door.resumes == [] and "not active" in api.of("answer")[-1][2]


class TestWritingWhileAnApprovalIsWaiting:
    async def test_the_person_is_pointed_at_the_buttons_and_nothing_is_sent_to_the_agent(self):
        bot, api, door, store = make_bot()
        await pending_token(bot, door, store)

        await bot.handle_update({"update_id": 2, "message": message("hello?")})

        assert len(door.turns) == 1
        assert api.sent_texts()[-1] == telegram_channel._ASK_FIRST

    async def test_after_the_approval_expired_the_stale_pause_is_cleared_and_the_message_goes_through(self):
        bot, api, door, store = make_bot()
        await pending_token(bot, door, store)
        store._redis.kv.clear()  # every record expired
        refusal = {"type": "error", "content": "This conversation has a pending approval", "code": "pending_approval"}
        door.scripts += [[refusal], [{"type": "token", "content": "Here you go."}]]

        await bot.handle_update({"update_id": 2, "message": message("hello?")})

        assert door.cancels == [(THREAD, True)]  # waited for: the new turn must not arrive before the cancel is done
        assert [t["replay_key"] for t in door.turns[1:]] == ["telegram:ecorp:update:2", "telegram:ecorp:update:2:cleared"]
        assert api.sent_texts()[-1] == "Here you go."

    async def test_a_pause_that_cannot_be_cleared_shows_the_refusal_instead_of_looping(self):
        bot, api, door, store = make_bot()
        await pending_token(bot, door, store)
        store._redis.kv.clear()
        door.cancel_fails = True
        door.scripts.append([{"type": "error", "content": "pending approval", "code": "pending_approval"}])

        await bot.handle_update({"update_id": 2, "message": message("hello?")})

        assert len(door.turns) == 2
        assert api.sent_texts()[-1] == "pending approval"


class TestCancel:
    async def test_cancelling_a_waiting_approval_drops_it_and_takes_the_buttons_away(self):
        bot, api, door, store = make_bot()
        await pending_token(bot, door, store)
        before = _count(metrics.channel_telegram_approval_total, outcome="cancelled")

        await bot.handle_update({"update_id": 2, "message": message("/cancel")})

        assert door.cancels == [(THREAD, False)]
        assert await store.for_thread(THREAD) is None
        assert "Cancelled" in api.of("edit")[-1][3] and api.of("edit")[-1][4] == {"inline_keyboard": []}
        assert api.sent_texts()[-1] == "Cancelled."
        assert _count(metrics.channel_telegram_approval_total, outcome="cancelled") == before + 1

    async def test_cancelling_with_nothing_waiting_still_asks_the_agent_to_stop(self):
        bot, api, door, _ = make_bot()

        await bot.handle_update({"update_id": 1, "message": message("/cancel@my_bot")})

        assert door.cancels == [(THREAD, False)]
        assert "stop" in api.sent_texts()[-1]

    async def test_a_cancel_the_door_refused_tells_the_person(self):
        bot, api, _, _ = make_bot(FakeDoor(cancel_fails=True))

        await bot.handle_update({"update_id": 1, "message": message("/cancel")})

        assert "could not reach" in api.sent_texts()[-1]


class TestStreamingTheReply:
    async def test_the_reply_grows_in_one_message_and_the_last_word_is_exact(self, monkeypatch):
        monkeypatch.setattr(settings, "telegram_stream_replies", True)
        monkeypatch.setattr(settings, "telegram_stream_edit_interval_seconds", 30.0)  # nothing but the first and the final write goes out
        events = [{"type": "token", "content": c} for c in ("Hel", "lo", " there")]
        bot, api, _, _ = make_bot(FakeDoor([events]))

        await bot.handle_update({"update_id": 1, "message": message()})

        assert api.sent_texts() == ["Hel"]
        assert api.of("edit")[-1][3] == "Hello there"


class TestRun:
    async def test_refuses_to_start_without_a_bot_token(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            await telegram_channel.run()

    async def test_refuses_to_start_in_identity_mode_without_the_secret_the_people_are_derived_from(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "identity")
        monkeypatch.setattr(settings, "telegram_linking", False)
        monkeypatch.setattr(settings, "telegram_person_secret", "too short")

        with pytest.raises(SystemExit, match="TELEGRAM_PERSON_SECRET"):
            await telegram_channel.run()

    async def test_a_linking_bot_does_not_need_the_derivation_secret_but_does_need_its_own_settings(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "identity")
        monkeypatch.setattr(settings, "telegram_linking", True)
        monkeypatch.setattr(settings, "telegram_person_secret", "")
        monkeypatch.setattr(settings, "telegram_link_public_url", "")

        with pytest.raises(SystemExit, match="TELEGRAM_LINK_PUBLIC_URL"):
            await telegram_channel.run()

    async def test_validates_agent_domain_before_polling(self, monkeypatch):
        """AGENT_DOMAIN is checked BEFORE the loop starts, so a typo'd domain fails at startup instead of sending to a stream no worker pool reads."""
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "no-such-domain")

        with pytest.raises(RuntimeError, match="no-such-domain"):
            await telegram_channel.run()

    async def test_a_webhook_bot_with_a_weak_secret_does_not_start(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(settings, "telegram_mode", "webhook")
        monkeypatch.setattr(settings, "telegram_webhook_url", "https://bot.example.com/telegram/webhook")
        monkeypatch.setattr(settings, "telegram_webhook_secret", "short")

        with pytest.raises(SystemExit, match="TELEGRAM_WEBHOOK_SECRET"):
            await telegram_channel.run()


class TestPolling:
    class _Api:
        def __init__(self, batches):
            self.batches = list(batches)
            self.offsets = []

        async def get_updates(self, offset, timeout):
            self.offsets.append(offset)
            if not self.batches:
                raise ConnectionError("stop")
            return self.batches.pop(0)

    async def test_updates_go_to_the_inbox_and_the_offset_moves_past_them_once_they_are_there(self, monkeypatch):
        redis = FakeRedis()
        stop = asyncio.Event()
        api = self._Api([[{"update_id": 5, "message": message("a")}, {"update_id": 6, "message": message("b")}]])

        async def stop_instead_of_sleeping(_):
            stop.set()

        monkeypatch.setattr(telegram_channel.asyncio, "sleep", stop_instead_of_sleeping)
        await telegram_channel._poll(api, redis, Inbox(redis, "ecorp"), stop)

        assert await telegram_channel._load_offset(redis, "ecorp") == 7
        assert api.offsets == [0, 7]
        assert len(redis.lists["telegram:inbox:ecorp"]) == 2

    async def test_a_push_that_fails_leaves_the_offset_alone_so_the_updates_come_again(self, monkeypatch):
        redis = FakeRedis()
        stop = asyncio.Event()

        class _BrokenInbox:
            async def push(self, update):
                raise ConnectionError("redis is down")

        async def stop_instead_of_sleeping(_):
            stop.set()

        monkeypatch.setattr(telegram_channel.asyncio, "sleep", stop_instead_of_sleeping)
        await telegram_channel._poll(self._Api([[{"update_id": 5, "message": message("a")}]]), redis, _BrokenInbox(), stop)

        assert await telegram_channel._load_offset(redis, "ecorp") == 0


class TestOffsetPersistence:
    """`_load_offset`/`_save_offset` are the fix for a real bug: `offset` used to live only in a local variable, so any process restart reset it to 0
    and Telegram would redeliver every update it still remembers, each producing a fresh duplicate turn and reply to a real user."""

    async def test_load_returns_zero_when_nothing_persisted_yet(self):
        assert await telegram_channel._load_offset(FakeRedis(), "ecorp") == 0

    async def test_save_then_load_round_trips(self):
        client = FakeRedis()
        await telegram_channel._save_offset(client, "ecorp", 12345)
        assert await telegram_channel._load_offset(client, "ecorp") == 12345
