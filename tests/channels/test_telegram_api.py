"""The Bot API client (app/adapters/inbound/channels/telegram_api.py). The request shapes are the ones the Bot API documents (read 2026-11-02, see the module's docstring);
nothing here reaches Telegram."""
import logging

import pytest

from app.adapters.inbound.channels.telegram_api import (
    CALLBACK_ANSWER_LIMIT,
    MESSAGE_CHAR_LIMIT,
    BotApi,
    split_text,
)

TOKEN = "123456:SECRET-bot-token"


class _Reply:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _Http:
    def __init__(self, *replies, raises=None):
        self.replies = list(replies)
        self.raises = raises
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        if self.raises:
            raise self.raises
        return self.replies.pop(0) if self.replies else _Reply({"ok": True, "result": {"message_id": 7}})


def api(http):
    return BotApi(http, TOKEN)


class TestSplitText:
    def test_a_short_text_is_one_part(self):
        assert split_text("hi") == ["hi"]

    def test_a_long_text_is_split_not_cut(self):
        parts = split_text("x" * (MESSAGE_CHAR_LIMIT * 2 + 3))

        assert [len(p) for p in parts] == [MESSAGE_CHAR_LIMIT, MESSAGE_CHAR_LIMIT, 3]


class TestSending:
    async def test_a_message_goes_to_the_bot_url_with_its_keyboard_and_returns_its_id(self):
        http = _Http()

        message_id = await api(http).send_message(5, "hello", reply_markup={"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]})

        assert message_id == 7
        url, payload = http.posts[0]
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert payload == {"chat_id": 5, "text": "hello", "reply_markup": {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}}

    async def test_a_long_text_is_sent_in_order_in_parts_and_the_first_id_is_returned(self):
        http = _Http(_Reply({"ok": True, "result": {"message_id": 11}}), _Reply({"ok": True, "result": {"message_id": 12}}))

        first = await api(http).send_text(5, "x" * (MESSAGE_CHAR_LIMIT + 1))

        assert first == 11 and [len(p["text"]) for _, p in http.posts] == [MESSAGE_CHAR_LIMIT, 1]

    async def test_an_empty_text_sends_nothing_because_telegram_refuses_it(self):
        http = _Http()

        assert await api(http).send_text(5, "") is None
        assert http.posts == []

    async def test_a_failed_send_is_swallowed_and_never_logs_the_token(self, caplog):
        http = _Http(raises=RuntimeError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage"))

        with caplog.at_level(logging.WARNING):
            assert await api(http).send_message(5, "hi") is None  # must not raise: one bad chat cannot stop the loop

        assert any(r.__dict__.get("error_class") == "RuntimeError" for r in caplog.records)  # the class, so an operator can triage
        assert TOKEN not in caplog.text and all(TOKEN not in str(r.__dict__) for r in caplog.records)

    async def test_a_refused_send_returns_none(self):
        assert await api(_Http(_Reply({}, status=403))).send_message(5, "hi") is None


class TestEditingAndAnswering:
    async def test_an_edit_always_states_the_keyboard_so_none_means_no_buttons(self):
        http = _Http()

        assert await api(http).edit_message(5, 9, "done") is True

        assert http.posts[0][1] == {"chat_id": 5, "message_id": 9, "text": "done", "reply_markup": {"inline_keyboard": []}}

    async def test_a_refused_edit_is_false_not_an_exception(self):
        assert await api(_Http(_Reply({}, status=400))).edit_message(5, 9, "done") is False

    async def test_an_answer_is_cut_to_telegrams_limit_and_shows_as_an_alert_only_when_asked(self):
        http = _Http()

        await api(http).answer_callback("cb", "y" * 500, alert=True)
        await api(http).answer_callback("cb")

        assert len(http.posts[0][1]["text"]) == CALLBACK_ANSWER_LIMIT and http.posts[0][1]["show_alert"] is True
        assert http.posts[1][1] == {"callback_query_id": "cb"}

    async def test_typing_is_a_chat_action(self):
        http = _Http()

        await api(http).typing(5)

        assert http.posts[0][0].endswith("/sendChatAction") and http.posts[0][1] == {"chat_id": 5, "action": "typing"}


class TestPolling:
    async def test_updates_are_asked_for_by_type_and_junk_is_filtered_out(self):
        http = _Http(_Reply({"ok": True, "result": [{"update_id": 1}, "junk", {"no_id": True}, {"update_id": "x"}, {"update_id": 2}]}))

        updates = await api(http).get_updates(40, 30)

        assert [u["update_id"] for u in updates] == [1, 2]
        assert http.posts[0][1] == {"offset": 40, "timeout": 30, "allowed_updates": ["message", "callback_query"]}

    async def test_a_failed_poll_raises_so_the_loop_can_back_off(self):
        with pytest.raises(RuntimeError):
            await api(_Http(_Reply({}, status=502))).get_updates(0, 30)


class TestWebhook:
    async def test_it_is_registered_with_the_secret_and_only_the_update_types_the_bot_reads(self):
        http = _Http(_Reply({"ok": True, "result": True}))

        await api(http).set_webhook("https://bot.example.com/telegram/webhook", "s" * 40)

        assert http.posts[0][1] == {
            "url": "https://bot.example.com/telegram/webhook",
            "secret_token": "s" * 40,
            "allowed_updates": ["message", "callback_query"],
        }

    async def test_a_registration_telegram_refuses_stops_the_start_up(self):
        with pytest.raises(RuntimeError, match="refused"):
            await api(_Http(_Reply({"ok": False}))).set_webhook("https://x/telegram/webhook", "s" * 40)

    async def test_polling_removes_a_webhook_left_by_an_earlier_run_and_keeps_pending_updates(self):
        http = _Http()

        await api(http).delete_webhook()

        assert http.posts[0][0].endswith("/deleteWebhook") and http.posts[0][1] == {"drop_pending_updates": False}
