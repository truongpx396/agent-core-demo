"""Tests for scripts/followup_sweep.py — same hermetic-DI approach as
tests/scripts/test_ops_digest.py: a fake LLM via `llm=`, and
store/notify monkeypatched so this never touches a real Postgres, network, or
filesystem sink. The metered call's usage event is captured by tests/conftest.py's
autouse `usage_event_sink`.

`run_followup_sweep` is `async def` now (awaits `store.due_followups`,
`chat.ainvoke`, `notify.post_to_team_channel`,
`store.mark_followup_done` — all real I/O), so every call below runs
through `asyncio.run(...)`.
"""
import asyncio

import pytest

from app.agent import gateway
from scripts import followup_sweep


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.invocations = []
        self.invoked_kwargs = []

    async def ainvoke(self, messages, **kwargs):
        self.invocations.append(messages)
        self.invoked_kwargs.append(kwargs)
        return next(self._responses)


def test_build_followup_prompt_includes_lead_and_note():
    prompt = followup_sweep.build_followup_prompt("Jordan", "jordan@example.com", "check in on pricing")
    assert "Jordan" in prompt
    assert "jordan@example.com" in prompt
    assert "check in on pricing" in prompt


def test_run_followup_sweep_returns_empty_when_nothing_is_due(monkeypatch):
    async def fake_due_followups(tenant, as_of):
        return []

    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)

    drafts = asyncio.run(followup_sweep.run_followup_sweep(llm=_FakeChat([])))

    assert drafts == []


def test_run_followup_sweep_drafts_posts_and_marks_each_followup_done(monkeypatch):
    due_items = [
        {"id": 1, "due_at": "2099-01-01", "note": "check pricing", "contact": "a@example.com", "lead_name": "A"},
        {"id": 2, "due_at": "2099-01-01", "note": "send demo link", "contact": "b@example.com", "lead_name": "B"},
    ]

    async def fake_due_followups(tenant, as_of):
        return due_items

    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)

    marked_done = []

    async def fake_mark_followup_done(tenant, followup_id):
        marked_done.append(followup_id)

    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", fake_mark_followup_done)

    posted = []

    async def fake_post_to_team_channel(channel, message):
        posted.append((channel, message))

    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", fake_post_to_team_channel)



    fake_chat = _FakeChat([_FakeResponse("Hi A, checking in on pricing!"), _FakeResponse("Hi B, here's the demo link!")])
    drafts = asyncio.run(followup_sweep.run_followup_sweep(llm=fake_chat))

    assert drafts == ["Hi A, checking in on pricing!", "Hi B, here's the demo link!"]
    assert marked_done == [1, 2]
    assert len(posted) == 2
    assert all(channel == "sales-followups" for channel, _ in posted)
    assert "Hi A, checking in on pricing!" in posted[0][1]


def test_run_followup_sweep_tells_the_gateway_whose_call_it_is(monkeypatch):
    """Otherwise the gateway's spend log shows this cron's spend as an anonymous caller
    (app/agent/gateway.py)."""
    due_items = [{"id": 1, "due_at": "2099-01-01", "note": "n", "contact": "a@example.com", "lead_name": "A"}]

    async def fake_due_followups(tenant, as_of):
        return due_items

    async def fake_mark_followup_done(tenant, followup_id):
        return None

    async def fake_post_to_team_channel(channel, message):
        return None


    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)
    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", fake_mark_followup_done)
    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", fake_post_to_team_channel)

    fake_chat = _FakeChat([_FakeResponse("Hi A!")])
    asyncio.run(followup_sweep.run_followup_sweep(llm=fake_chat))

    assert fake_chat.invoked_kwargs == [gateway.call_identity(followup_sweep._CRON_CTX)]
    assert fake_chat.invoked_kwargs[0]["user"] == gateway.end_user_id(followup_sweep.DEFAULT_TENANT)


def test_run_followup_sweep_records_one_cron_usage_event_per_draft(monkeypatch, usage_event_sink):
    due_items = [
        {"id": 7, "due_at": "2099-01-01", "note": "n", "contact": "a@example.com", "lead_name": "A"},
        {"id": 8, "due_at": "2099-01-01", "note": "n", "contact": "b@example.com", "lead_name": "B"},
    ]

    async def fake_due_followups(tenant, as_of):
        return due_items

    async def fake_mark_followup_done(tenant, followup_id):
        return None

    async def fake_post_to_team_channel(channel, message):
        return None


    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)
    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", fake_mark_followup_done)
    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", fake_post_to_team_channel)

    usage = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    chat = _FakeChat([_FakeResponse("Hi A!", usage_metadata=usage), _FakeResponse("Hi B!", usage_metadata=usage)])
    asyncio.run(followup_sweep.run_followup_sweep(llm=chat))

    assert [(e["kind"], e["thread_id"]) for e in usage_event_sink] == [
        ("cron", "followup-sweep:7"),
        ("cron", "followup-sweep:8"),
    ]
    assert len({e["event_id"] for e in usage_event_sink}) == 2


def test_run_followup_sweep_records_usage_when_tokens_are_reported(monkeypatch, usage_event_sink):
    due_items = [{"id": 1, "due_at": "2099-01-01", "note": "n", "contact": "a@example.com", "lead_name": "A"}]

    async def fake_due_followups(tenant, as_of):
        return due_items

    async def fake_mark_followup_done(tenant, followup_id):
        return None

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)
    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", fake_mark_followup_done)
    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", fake_post_to_team_channel)

    fake_chat = _FakeChat([_FakeResponse("draft", usage_metadata={"total_tokens": 17})])
    asyncio.run(followup_sweep.run_followup_sweep(llm=fake_chat))

    assert [event["total_tokens"] for event in usage_event_sink] == [17]


def test_run_followup_sweep_records_the_priced_cost_of_each_draft(monkeypatch, usage_event_sink):
    """Spec 008 A2: same as the digest — the job prices its own model call."""
    from app.agent import pricing
    from app.core.config import CHAT_MODEL

    async def priced_fetch():
        return [
            {
                "model_name": CHAT_MODEL,
                "model_info": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05},
            }
        ]

    due_items = [{"id": 1, "due_at": "2099-01-01", "note": "n", "contact": "a@example.com", "lead_name": "A"}]

    async def fake_due_followups(tenant, as_of):
        return due_items

    async def fake_mark_followup_done(tenant, followup_id):
        return None

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(pricing, "_fetch_model_info", priced_fetch)
    monkeypatch.setattr(followup_sweep.store, "due_followups", fake_due_followups)
    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", fake_mark_followup_done)
    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", fake_post_to_team_channel)
    usage = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}

    asyncio.run(followup_sweep.run_followup_sweep(llm=_FakeChat([_FakeResponse("draft", usage_metadata=usage)])))

    (event,) = usage_event_sink
    assert event["cost_usd"] == pytest.approx(0.0075)  # 1000 * 2.5e-06 + 500 * 1e-05
