"""The state and words behind the Telegram approval buttons (app/adapters/inbound/channels/telegram/approvals.py, specs/021 FR-001). The flow that uses them, and the
cases where a tap must do nothing, are in test_bot.py."""
import json

import pytest

from app.adapters.inbound.channels.telegram.approvals import (
    ApprovalStore,
    Pending,
    callback_data,
    keyboard,
    parse_callback,
    render,
)
from tests.adapters.inbound.channels.telegram.fakes import FakeRedis


class TestTheButtons:
    def test_a_token_and_a_decision_survive_the_round_trip(self):
        assert parse_callback(callback_data("tok-1_A", True)) == ("tok-1_A", True)
        assert parse_callback(callback_data("tok-1_A", False)) == ("tok-1_A", False)

    @pytest.mark.parametrize("data", [None, 5, "", "ap", "ap::y", "ap:t:z", "xx:t:y", "ap:t:y:extra"])
    def test_anything_this_bot_did_not_make_is_not_a_decision(self, data):
        assert parse_callback(data) is None

    def test_the_data_fits_telegrams_64_byte_limit_with_a_real_token(self):
        import secrets

        assert len(callback_data(secrets.token_urlsafe(16), True).encode()) <= 64

    def test_the_keyboard_has_one_approve_and_one_reject_for_the_same_token(self):
        buttons = keyboard("tok")["inline_keyboard"][0]

        assert [parse_callback(b["callback_data"]) for b in buttons] == [("tok", True), ("tok", False)]


class TestWhatThePersonIsAsked:
    def test_every_pending_call_is_named_with_its_arguments(self):
        text = render([{"name": "add_note", "args": {"title": "hi", "n": 2}}, {"name": "send_email", "args": {}}])

        assert "add_note(title=hi, n=2)" in text and "send_email()" in text
        assert "approval" in text

    def test_an_argument_is_clipped_so_a_pasted_document_cannot_flood_the_chat(self):
        text = render([{"name": "add_note", "args": {"body": "x" * 5000}}])

        assert len(text) < 400 and text.count("x") < 200 and "…" in text

    def test_a_long_list_of_calls_still_fits_one_message(self):
        assert len(render([{"name": f"tool_{i}", "args": {"a": "y" * 150}} for i in range(100)])) <= 3000

    def test_a_call_with_odd_shapes_does_not_break_the_message(self):
        assert "?" in render([{"args": "not a dict"}])


class TestTheStore:
    def make(self):
        redis = FakeRedis()
        return ApprovalStore(redis, "ecorp", ttl_seconds=900), redis

    async def test_a_created_approval_is_found_by_token_and_by_conversation(self):
        store, _ = self.make()

        pending = await store.create(user_id=42, chat_id=1, thread_id="telegram:p:1", text="ask")

        assert await store.get(pending.token) == pending
        assert await store.for_thread("telegram:p:1") == pending
        assert await store.for_thread("telegram:q:1") is None

    async def test_it_lives_for_the_configured_time_and_the_message_id_keeps_that_lifetime(self):
        store, redis = self.make()
        pending = await store.create(user_id=1, chat_id=1, thread_id="t", text="ask")

        updated = await store.attach_message(pending, 77)

        assert updated.message_id == 77 and (await store.get(pending.token)).message_id == 77
        assert set(redis.ttls.values()) == {900}

    async def test_the_tokens_are_not_guessable_or_repeated(self):
        store, _ = self.make()

        tokens = {(await store.create(user_id=1, chat_id=1, thread_id=f"t{i}", text="ask")).token for i in range(50)}

        assert len(tokens) == 50 and all(len(t) >= 20 for t in tokens)

    async def test_exactly_one_caller_gets_to_decide(self):
        store, _ = self.make()

        assert [await store.claim("tok"), await store.claim("tok")] == [True, False]

    async def test_a_released_claim_can_be_taken_again(self):
        store, _ = self.make()
        await store.claim("tok")

        await store.release("tok")

        assert await store.claim("tok") is True

    async def test_finishing_forgets_the_approval_but_not_that_it_was_decided(self):
        store, _ = self.make()
        pending = await store.create(user_id=1, chat_id=1, thread_id="t", text="ask")
        await store.claim(pending.token)

        await store.finish(pending)

        assert await store.get(pending.token) is None and await store.for_thread("t") is None
        assert await store.claim(pending.token) is False

    async def test_approvals_of_two_domains_do_not_see_each_other(self):
        redis = FakeRedis()
        ecorp, support = ApprovalStore(redis, "ecorp", ttl_seconds=60), ApprovalStore(redis, "support", ttl_seconds=60)
        pending = await ecorp.create(user_id=1, chat_id=1, thread_id="t", text="ask")

        assert await support.get(pending.token) is None and await support.for_thread("t") is None

    async def test_a_record_that_cannot_be_read_is_as_good_as_absent(self):
        store, redis = self.make()
        pending = await store.create(user_id=1, chat_id=1, thread_id="t", text="ask")
        redis.kv[f"telegram:approval:ecorp:{pending.token}"] = json.dumps({"unexpected": 1})

        assert await store.get(pending.token) is None
        redis.kv[f"telegram:approval:ecorp:{pending.token}"] = "not json"
        assert await store.get(pending.token) is None

    def test_a_pending_approval_is_immutable(self):
        pending = Pending("t", 1, 1, "th", "ask")

        with pytest.raises(AttributeError):
            pending.user_id = 2  # type: ignore[misc]  # the point of the test
