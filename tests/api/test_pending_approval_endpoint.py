"""GET /chat/sessions/{thread_id}/pending_approval — the endpoint the session
switcher calls to re-show the approve/reject banner for a thread still paused
at the approval gate. It had no API-level test at all (Principle II: a pause
must be durable and discoverable, and Principle I: not discoverable by anyone
but its owner).

`session_belongs_to` is the entire authorization boundary — the shared
checkpointer `get_pending_approval` reads has no tenant, principal or domain of
its own — so each refusal below must (a) be the identical 404 whether the thread
is someone else's or does not exist, and (b) never call `get_pending_approval`.
Mirrors the transcript endpoint's tests in tests/api/test_api.py.
"""
import pytest
from fastapi import HTTPException

from app.agent import sessions
from app.api import main as api
from tests.conftest import TEST_CTX


@pytest.fixture
def owner(monkeypatch):
    """`session_belongs_to` replaced by a one-thread ownership record."""
    record = {"thread": "paused-1", "ctx": TEST_CTX, "domain": "support", "asked": []}

    async def belongs(ctx, thread_id, domain="ecorp"):
        record["asked"].append((ctx["tenant"], ctx["principal"], thread_id, domain))
        return (thread_id, ctx, domain) == (record["thread"], record["ctx"], record["domain"])

    monkeypatch.setattr(sessions, "session_belongs_to", belongs)
    return record


@pytest.fixture
def pending(monkeypatch):
    calls = []

    async def get_pending_approval(thread_id):
        calls.append(thread_id)
        return {"tool_calls": [{"name": "add_note", "args": {"title": "T"}, "id": "c1"}], "resumable": True}

    monkeypatch.setattr(api, "get_pending_approval", get_pending_approval)
    return calls


async def test_the_owner_gets_the_pending_tool_calls_and_whether_they_can_be_resumed(owner, pending):
    result = await api.chat_session_pending_approval("paused-1", ctx=TEST_CTX, domain="support")

    assert result["resumable"] is True
    assert result["tool_calls"][0]["name"] == "add_note"
    assert pending == ["paused-1"]


async def test_a_thread_that_is_not_paused_reports_none(owner, monkeypatch):
    async def not_paused(thread_id):
        return None

    monkeypatch.setattr(api, "get_pending_approval", not_paused)

    assert await api.chat_session_pending_approval("paused-1", ctx=TEST_CTX, domain="support") is None


@pytest.mark.parametrize(
    "ctx,thread,domain",
    [
        ({"tenant": "acme", "principal": "mallory", "claims": {}}, "paused-1", "support"),  # another principal
        ({"tenant": "evil-co", "principal": TEST_CTX["principal"], "claims": {}}, "paused-1", "support"),  # another tenant
        (TEST_CTX, "paused-1", "sales"),  # the owner, under another domain
        (TEST_CTX, "no-such-thread", "support"),  # nobody's
    ],
    ids=["other-principal", "other-tenant", "other-domain", "nonexistent"],
)
async def test_everyone_else_gets_the_same_404_and_the_pause_is_never_read(owner, pending, ctx, thread, domain):
    with pytest.raises(HTTPException) as exc_info:
        await api.chat_session_pending_approval(thread, ctx=ctx, domain=domain)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "session not found"
    assert pending == [], "an unauthorized caller must not even cause the checkpoint to be read"
