"""Tests for scripts/followup_sweep.py — same hermetic-DI approach as
tests/scripts/test_ops_digest.py: a fake LLM via `llm=`, and
store/notify/record_usage monkeypatched so this never touches a real
Postgres, network, or filesystem sink.
"""
from scripts import followup_sweep


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.invocations = []

    def invoke(self, messages):
        self.invocations.append(messages)
        return next(self._responses)


def test_build_followup_prompt_includes_lead_and_note():
    prompt = followup_sweep.build_followup_prompt("Jordan", "jordan@example.com", "check in on pricing")
    assert "Jordan" in prompt
    assert "jordan@example.com" in prompt
    assert "check in on pricing" in prompt


def test_run_followup_sweep_returns_empty_when_nothing_is_due(monkeypatch):
    monkeypatch.setattr(followup_sweep.store, "due_followups", lambda tenant, as_of: [])

    drafts = followup_sweep.run_followup_sweep(llm=_FakeChat([]))

    assert drafts == []


def test_run_followup_sweep_drafts_posts_and_marks_each_followup_done(monkeypatch):
    due_items = [
        {"id": 1, "due_at": "2099-01-01", "note": "check pricing", "contact": "a@example.com", "lead_name": "A"},
        {"id": 2, "due_at": "2099-01-01", "note": "send demo link", "contact": "b@example.com", "lead_name": "B"},
    ]
    monkeypatch.setattr(followup_sweep.store, "due_followups", lambda tenant, as_of: due_items)

    marked_done = []
    monkeypatch.setattr(
        followup_sweep.store, "mark_followup_done", lambda tenant, followup_id: marked_done.append(followup_id)
    )

    posted = []
    monkeypatch.setattr(
        followup_sweep.notify,
        "post_to_team_channel",
        lambda channel, message: posted.append((channel, message)),
    )
    monkeypatch.setattr(followup_sweep, "record_usage", lambda *a, **kw: None)

    fake_chat = _FakeChat([_FakeResponse("Hi A, checking in on pricing!"), _FakeResponse("Hi B, here's the demo link!")])
    drafts = followup_sweep.run_followup_sweep(llm=fake_chat)

    assert drafts == ["Hi A, checking in on pricing!", "Hi B, here's the demo link!"]
    assert marked_done == [1, 2]
    assert len(posted) == 2
    assert all(channel == "sales-followups" for channel, _ in posted)
    assert "Hi A, checking in on pricing!" in posted[0][1]


def test_run_followup_sweep_records_usage_when_tokens_are_reported(monkeypatch):
    due_items = [{"id": 1, "due_at": "2099-01-01", "note": "n", "contact": "a@example.com", "lead_name": "A"}]
    monkeypatch.setattr(followup_sweep.store, "due_followups", lambda tenant, as_of: due_items)
    monkeypatch.setattr(followup_sweep.store, "mark_followup_done", lambda tenant, followup_id: None)
    monkeypatch.setattr(followup_sweep.notify, "post_to_team_channel", lambda channel, message: None)

    recorded = {}
    monkeypatch.setattr(
        followup_sweep,
        "record_usage",
        lambda ctx, thread_id, model_alias, total_tokens: recorded.setdefault("total_tokens", total_tokens),
    )

    fake_chat = _FakeChat([_FakeResponse("draft", usage_metadata={"total_tokens": 17})])
    followup_sweep.run_followup_sweep(llm=fake_chat)

    assert recorded["total_tokens"] == 17
