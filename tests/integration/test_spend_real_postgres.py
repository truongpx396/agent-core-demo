"""What a tenant has spent, read from the usage events, against a REAL Postgres (specs/010 T030).

The dollar caps and `GET /usage` used to sum `usage_ledger` (one row per turn) and now sum `usage_events` (one row per
model call). tests/agent/test_spend.py proves the statement's shape through a fake connection; only a real table shows
what the shipped schema does with it:

  * the sum never crosses tenants and, when narrowed, never crosses people: the property every budget decision rests on;
  * the window honours `since`, so a rolling 24h is not a calendar day;
  * an UNPRICED call (NULL cost) adds its tokens and no dollars, and does not turn the whole sum NULL;
  * a cap trips on events ALONE, with no ledger row anywhere: this is the test that fails if the caps still read the ledger;
  * a ledger row ALONE is no longer counted, which pins the cutover (and is why `make usage-events-carry-over` exists);
  * the writer's columns and the reader's agree (a call written by the real writer is what the cap sees);
  * the history `make usage-events-carry-over` copied is what the cap then reads, in the window it was spent in;
  * the window read can use `(tenant, occurred_at)`, proven the way the ledger's index was: with sequential scans off.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import budgets, metering, spend, usage_events, usage_ledger
from scripts import usage_events_carry_over as carry
from tests.containers import ensure_postgres
from tests.integration.usage_seed import purge_events, seed_event, seed_ledger_row

# Two tests here write `usage_ledger` rows (one of them three years old, the history a carry-over copies) and the ledger sweep tests
# (test_ledger_real_postgres.py) delete old ledger rows across tenants up to a ceiling: same xdist group, rows removed afterwards.
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("usage_ledger_real_postgres")]

_REAL_INSERT = usage_events._insert


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    """Point the read (and the holds the cap also reads, and the writer) at the real database. Each module's OWN
    `get_connection` binding: tests/conftest.py's autouse `mock_appdata_postgres` patches the same names."""

    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    for module in (spend, usage_ledger, usage_events, carry):
        monkeypatch.setattr(module, "get_connection", get_connection)
    monkeypatch.setattr(usage_events, "_insert", _REAL_INSERT)  # the autouse sink replaced it
    return get_connection


@pytest.fixture
async def tenant(real_appdata) -> str:
    """A tenant of this test's own; everything it wrote, ledger rows and events, is removed afterwards (see `purge_events`)."""
    name = f"acme-{uuid.uuid4().hex[:8]}"
    yield name
    async with real_appdata() as conn:
        await conn.execute("DELETE FROM usage_ledger WHERE tenant = %s", (name,))
        await purge_events(conn, name)


def _ctx(tenant: str, principal: str = "alice") -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


async def test_it_sums_only_this_tenant(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        await seed_event(conn, tenant, cost_usd=0.10, total_tokens=100)
        await seed_event(conn, tenant, cost_usd=0.20, total_tokens=200)
        await seed_event(conn, other, cost_usd=9.99, total_tokens=9999)

    summary = await spend.usage_summary(tenant)

    assert summary["total_tokens"] == 300
    assert summary["total_cost_usd"] == pytest.approx(0.30)


async def test_it_narrows_to_one_principal_inside_a_tenant(tenant, real_appdata):
    async with real_appdata() as conn:
        await seed_event(conn, tenant, principal="alice", cost_usd=0.10)
        await seed_event(conn, tenant, principal="bob", cost_usd=0.70)

    assert (await spend.usage_summary(tenant, principal="alice"))["total_cost_usd"] == pytest.approx(0.10)
    assert (await spend.usage_summary(tenant))["total_cost_usd"] == pytest.approx(0.80)


async def test_since_is_a_rolling_window_not_a_calendar_day(tenant, real_appdata):
    now = datetime.now(UTC)
    async with real_appdata() as conn:
        await seed_event(conn, tenant, cost_usd=5.00, occurred_at=now - timedelta(days=2))
        await seed_event(conn, tenant, cost_usd=0.50, occurred_at=now - timedelta(hours=1))

    in_window = await spend.usage_summary(tenant, since=now - timedelta(hours=24))
    all_time = await spend.usage_summary(tenant)

    assert in_window["total_cost_usd"] == pytest.approx(0.50)
    assert all_time["total_cost_usd"] == pytest.approx(5.50)


async def test_an_unpriced_call_adds_its_tokens_and_no_dollars_and_does_not_null_the_sum(tenant, real_appdata):
    async with real_appdata() as conn:
        await seed_event(conn, tenant, cost_usd=None, total_tokens=500)  # a model nobody has a price for
        await seed_event(conn, tenant, cost_usd=0.25, total_tokens=100)

    summary = await spend.usage_summary(tenant)

    assert summary["total_tokens"] == 600
    assert summary["total_cost_usd"] == pytest.approx(0.25)


async def test_a_tenant_with_nothing_recorded_has_spent_zero(tenant):
    assert await spend.usage_summary(tenant) == {"total_tokens": 0, "total_cost_usd": 0.0}


async def test_a_cap_trips_on_events_alone_with_no_ledger_row_anywhere(tenant, real_appdata):
    """The regression test for the cutover: before it the cap read `usage_ledger`, so spend that exists only as events
    (every follow-up, compaction and subagent call, a turn that never reached its ledger write) was invisible to it."""
    async with real_appdata() as conn:
        await seed_event(conn, tenant, cost_usd=6.00)
        cur = await conn.execute("SELECT count(*) FROM usage_ledger WHERE tenant = %s", (tenant,))
        assert (await cur.fetchone())[0] == 0, "control: there is no ledger row for this tenant"

    allowance = await budgets.check_allowance(
        _ctx(tenant), limits=[budgets.BudgetLimit("tenant", "day", 5.0)], fail_policy="closed"
    )

    assert allowance.refused is True and allowance.status == "exceeded"


async def test_a_cap_is_not_tripped_by_spend_inside_its_limit(tenant, real_appdata):
    async with real_appdata() as conn:
        await seed_event(conn, tenant, cost_usd=4.00)

    allowance = await budgets.check_allowance(
        _ctx(tenant), limits=[budgets.BudgetLimit("tenant", "day", 5.0)], fail_policy="closed"
    )

    assert allowance.refused is False


async def test_a_ledger_row_alone_is_no_longer_counted(tenant, real_appdata):
    """Pins the cutover: history that lives only in the ledger is invisible until `make usage-events-carry-over` copies
    it. Without this a revert of the read that forgot the carry-over would pass every other test."""
    async with real_appdata() as conn:
        await seed_ledger_row(conn, tenant, thread_id="thread-1", total_tokens=1000, cost_usd=50.0)

    assert await spend.usage_summary(tenant) == {"total_tokens": 0, "total_cost_usd": 0.0}


async def test_a_call_written_by_the_real_writer_is_what_the_cap_sees(tenant):
    """The writer's columns and the reader's agree: a call that goes through the choke point and the real INSERT is
    counted, tokens and all, by the read behind the caps."""
    config = {"configurable": {"ctx": _ctx(tenant), "thread_id": "thread-1"}}
    reply = AIMessage(content="an answer", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    await metering.metered_invoke(
        GenericFakeChatModel(messages=iter([reply])), [HumanMessage(content="q")],
        config=config, kind="chat", model_alias="chat",
    )

    assert (await spend.usage_summary(tenant))["total_tokens"] == 15


async def test_the_history_a_carry_over_copied_is_what_the_cap_then_reads(tenant, real_appdata):
    """The two halves meet. Ledger history older than the first real event is copied, and the read behind the caps then
    sees it; before the copy it does not, which is the monthly cap forgetting the month that the carry-over exists to prevent."""
    now = datetime.now(UTC)
    years_ago = now - timedelta(days=3 * 365)
    async with real_appdata() as conn:
        await conn.execute(
            "INSERT INTO usage_ledger (tenant, principal, thread_id, model_alias, total_tokens, cost_usd, recorded_at) "
            "VALUES (%s, 'alice', 'thread-1', 'chat', 4000, 40.0, %s)",
            (tenant, years_ago),
        )
        await seed_event(conn, tenant, cost_usd=0.5, total_tokens=10, occurred_at=now - timedelta(days=1))
    since = now - timedelta(days=4 * 365)
    before = await spend.usage_summary(tenant, since=since)

    await carry.run(tenant=tenant)

    after = await spend.usage_summary(tenant, since=since)
    recent = await spend.usage_summary(tenant, since=now - timedelta(days=30))
    assert before == {"total_tokens": 10, "total_cost_usd": pytest.approx(0.5)}
    assert after == {"total_tokens": 4010, "total_cost_usd": pytest.approx(40.5)}
    assert recent["total_cost_usd"] == pytest.approx(0.5), "history stays in the window it was spent in"


async def test_the_window_read_can_use_its_index(tenant, real_appdata):
    """With sequential scans off the planner must pick `(tenant, occurred_at)` for the cap's exact predicate: proof the
    index serves it, not merely that it exists (on a table this small it would otherwise choose a scan on cost alone)."""
    async with real_appdata() as conn:
        await conn.execute("SET enable_seqscan = off")
        cur = await conn.execute(
            "EXPLAIN SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM usage_events WHERE tenant = %s AND occurred_at >= %s",
            (tenant, datetime.now(UTC) - timedelta(hours=24)),
        )
        plan = "\n".join(row[0] for row in await cur.fetchall())

    assert "usage_events_tenant_occurred_idx" in plan
