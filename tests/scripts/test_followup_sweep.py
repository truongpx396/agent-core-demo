"""Tests for scripts/followup_sweep.py — same hermetic-DI approach as
tests/scripts/test_ops_digest.py: a fake LLM via `llm=`, and
store/notify/record_usage monkeypatched so this never touches a real
Postgres, network, or filesystem sink.

`run_followup_sweep` is `async def` now (awaits `store.due_followups`,
`chat.ainvoke`, `record_usage`, `notify.post_to_team_channel`,
`store.mark_followup_done` — all real I/O), so every call below runs
through `asyncio.run(...)`.
"""
import asyncio

from scripts import followup_sweep


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.invocations = []

    async def ainvoke(self, messages):
        self.invocations.append(messages)
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

    async def fake_record_usage(*a, **kw):
        return None

    monkeypatch.setattr(followup_sweep, "record_usage", fake_record_usage)

    fake_chat = _FakeChat([_FakeResponse("Hi A, checking in on pricing!"), _FakeResponse("Hi B, here's the demo link!")])
    drafts = asyncio.run(followup_sweep.run_followup_sweep(llm=fake_chat))

    assert drafts == ["Hi A, checking in on pricing!", "Hi B, here's the demo link!"]
    assert marked_done == [1, 2]
    assert len(posted) == 2
    assert all(channel == "sales-followups" for channel, _ in posted)
    assert "Hi A, checking in on pricing!" in posted[0][1]


def test_run_followup_sweep_records_usage_when_tokens_are_reported(monkeypatch):
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

    recorded = {}

    async def fake_record_usage(ctx, thread_id, model_alias, total_tokens):
        recorded["total_tokens"] = total_tokens

    monkeypatch.setattr(followup_sweep, "record_usage", fake_record_usage)

    fake_chat = _FakeChat([_FakeResponse("draft", usage_metadata={"total_tokens": 17})])
    asyncio.run(followup_sweep.run_followup_sweep(llm=fake_chat))

    assert recorded["total_tokens"] == 17
