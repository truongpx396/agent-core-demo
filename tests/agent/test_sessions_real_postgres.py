"""`sessions.claim_session` against a REAL Postgres (`integration` tier).

The hermetic tests in test_sessions.py pin the query *shape*; what they
cannot prove is the property the ownership gate depends on: that two callers
racing for a brand-new thread id cannot both be told they own it. That rests
on `INSERT … ON CONFLICT DO NOTHING` plus the unique index on
`chat_sessions.thread_id`, i.e. on Postgres's own behavior, which a fake
connection only echoes back (Constitution Principle VII: verify third-party
behavior against the real thing).

Each call gets its OWN connection, as in production where each request checks
out its own from the pool — a single shared connection would serialize the
race away and prove nothing.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest

from app.agent import sessions
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration


def _ctx(principal: str, tenant: str = "ecorp"):
    return {"tenant": tenant, "principal": principal, "claims": {}}


@pytest.fixture
def real_appdata(monkeypatch):
    url = ensure_postgres()["appdata_database_url"]

    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            yield conn

    monkeypatch.setattr(sessions, "get_connection", get_connection)
    return get_connection


async def test_the_first_claimant_owns_the_thread_and_nobody_else_does(real_appdata):
    thread_id = f"claim-{uuid.uuid4().hex}"

    assert await sessions.claim_session(_ctx("alice"), thread_id, "first words") is True
    assert await sessions.claim_session(_ctx("alice"), thread_id, "later words") is True, "the owner keeps access"
    assert await sessions.claim_session(_ctx("mallory"), thread_id, "hijack") is False
    assert await sessions.claim_session(_ctx("alice", tenant="evil-co"), thread_id, "hijack") is False
    assert await sessions.claim_session(_ctx("alice"), thread_id, "hi", domain="sales") is False

    # A refused or repeated claim changes nothing: same owner, the FIRST title.
    async with real_appdata() as conn:
        cur = await conn.execute(
            "SELECT tenant, principal, title, domain FROM chat_sessions WHERE thread_id = %s", (thread_id,)
        )
        assert await cur.fetchall() == [("ecorp", "alice", "first words", "ecorp")]


async def test_two_callers_racing_for_a_new_thread_id_cannot_both_win(real_appdata):
    for _ in range(5):
        thread_id = f"race-{uuid.uuid4().hex}"
        outcomes = await asyncio.gather(
            *(sessions.claim_session(_ctx(f"user-{i}"), thread_id, "hi") for i in range(20))
        )
        assert outcomes.count(True) == 1, outcomes


async def test_session_belongs_to_agrees_with_the_claim(real_appdata):
    thread_id = f"belongs-{uuid.uuid4().hex}"
    assert await sessions.session_belongs_to(_ctx("alice"), thread_id) is False, "nobody owns an unclaimed id"

    await sessions.claim_session(_ctx("alice"), thread_id, "hi")

    assert await sessions.session_belongs_to(_ctx("alice"), thread_id) is True
    assert await sessions.session_belongs_to(_ctx("mallory"), thread_id) is False
