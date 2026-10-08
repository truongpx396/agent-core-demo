"""The ledger-to-events carry-over against a REAL Postgres (scripts/usage_events_carry_over.py, specs/010 T030).

tests/scripts/test_usage_events_carry_over.py proves the loop through a scripted connection. What only a real database can
show is what the statements DO, and the cutover's safety rests on it:

  * history older than the first real event is copied, and NOTHING newer (those turns already have events: copying them
    would count them twice, which doubles a tenant's spend on the day it is switched on);
  * a re-run copies nothing (`ledger:<id>` is deterministic, the event id is the primary key);
  * after it, the sum the dollar caps take sees the old spend (the point of it all);
  * a carried row is history, not a call: it is never rated, charged or queued for export, so it cannot bill anyone or
    reach a provider;
  * a run that stopped at its ceiling is CONTINUED by the next one (the bug a scan restarted from id 0 would have had);
  * the empty-table cutoff is "now" (asked inside a transaction that is rolled back, so the shared table is untouched).

The container is shared across tests and xdist workers and the cutoff is global ("the first real event in the table"), so
every test uses its own tenant, passes it as the filter, and puts its "history" years in the past, older than anything any
other test seeds, and its "recent" rows after its own real event.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from scripts import usage_events_carry_over as carry
from tests.containers import ensure_postgres
from tests.integration.usage_seed import seed_event

# The ledger sweep tests (test_ledger_real_postgres.py) delete old `usage_ledger` rows ACROSS tenants, in batches, up to a ceiling, and
# these tests deliberately write old ledger rows (the history to carry), 3,000 of them in the race test: left behind or running at the
# same moment, they push the sweep test's own rows past its ceiling and it fails. Same xdist group, so the two never run concurrently.
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("usage_ledger_real_postgres")]

NOW = datetime.now(UTC)
YEARS_AGO = NOW - timedelta(days=3 * 365)


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    monkeypatch.setattr(carry, "get_connection", get_connection)
    return get_connection


@pytest.fixture
async def tenant(real_appdata) -> str:
    """A tenant of this test's own. Its ledger rows are removed afterwards (the ledger is not append-only; the events are, so
    those stay): old ledger rows left in the shared table are what the sweep tests would otherwise trip over."""
    name = f"acme-{uuid.uuid4().hex[:8]}"
    yield name
    async with real_appdata() as conn:
        await conn.execute("DELETE FROM usage_ledger WHERE tenant = %s OR tenant = %s", (name, f"{name}-other"))


async def _ledger(conn, tenant: str, *, recorded_at: datetime, cost: float, tokens: int = 100, principal: str = "alice") -> int:
    cur = await conn.execute(
        "INSERT INTO usage_ledger (tenant, principal, thread_id, model_alias, resolved_model, total_tokens, cost_usd, recorded_at) "
        "VALUES (%s, %s, 'thread-1', 'chat', 'gpt-test', %s, %s, %s) RETURNING id",
        (tenant, principal, tokens, cost, recorded_at),
    )
    return (await cur.fetchone())[0]


async def _carried(conn, tenant: str) -> list[tuple]:
    cur = await conn.execute(
        "SELECT event_id, kind, principal, thread_id, model_alias, resolved_model, total_tokens, cost_usd, occurred_at, "
        "recorded_at, input_tokens, output_tokens FROM usage_events WHERE tenant = %s AND event_id LIKE 'ledger:%%' "
        "ORDER BY event_id",
        (tenant,),
    )
    return await cur.fetchall()


async def test_history_before_the_first_real_event_is_carried_and_nothing_after_it(tenant, real_appdata):
    async with real_appdata() as conn:
        old = await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=1.50, tokens=300, principal="bob")
        older = await _ledger(conn, tenant, recorded_at=YEARS_AGO - timedelta(days=30), cost=0.25, tokens=50)
        await seed_event(conn, tenant, cost_usd=0.10, occurred_at=NOW - timedelta(days=1))  # the first REAL event
        recent = await _ledger(conn, tenant, recorded_at=NOW - timedelta(hours=12), cost=9.0)  # its turn has events

    result = await carry.run(tenant=tenant)
    async with real_appdata() as conn:
        rows = await _carried(conn, tenant)

    assert result.carried == 2 and result.carried_usd == Decimal("1.75") and result.complete is True
    assert [r[0] for r in rows] == sorted([f"ledger:{old}", f"ledger:{older}"])
    assert f"ledger:{recent}" not in [r[0] for r in rows], "a turn that already has events must not be counted twice"
    by_id = {r[0]: r for r in rows}
    (_, kind, principal, thread, alias, resolved, tokens, cost, occurred, recorded, in_tokens, out_tokens) = by_id[f"ledger:{old}"]
    assert (kind, principal, thread, alias, resolved, tokens) == ("chat", "bob", "thread-1", "chat", "gpt-test", 300)
    assert cost == Decimal("1.500000000000")
    assert occurred == YEARS_AGO == recorded, "history is counted in the window it was spent in, not the day it was copied"
    assert (in_tokens, out_tokens) == (0, 0), "the ledger never kept a split, and one is not invented"


async def test_a_rerun_carries_nothing_more(tenant, real_appdata):
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=1.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    first = await carry.run(tenant=tenant)
    second = await carry.run(tenant=tenant)

    assert first.carried == 1 and second.carried == 0 and second.carried_usd == 0
    assert carry.describe(second).startswith("Nothing to carry")
    async with real_appdata() as conn:
        assert len(await _carried(conn, tenant)) == 1


async def _sum(conn, tenant: str, since: datetime) -> tuple[int, Decimal]:
    """The sum the dollar caps take (`spend.usage_summary`, specs/010 T030b), as plain SQL so this file does not depend on that module."""
    cur = await conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE tenant = %s AND occurred_at >= %s",
        (tenant, since),
    )
    return await cur.fetchone()


async def test_the_events_sum_is_whole_after_the_carry_over(tenant, real_appdata):
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=40.0, tokens=4000)
        await seed_event(conn, tenant, cost_usd=0.5, total_tokens=10, occurred_at=NOW - timedelta(days=1))
    since = NOW - timedelta(days=4 * 365)
    async with real_appdata() as conn:
        before = await _sum(conn, tenant, since)

    await carry.run(tenant=tenant)

    async with real_appdata() as conn:
        after = await _sum(conn, tenant, since)
        in_a_recent_window = await _sum(conn, tenant, NOW - timedelta(days=30))
    assert before == (10, Decimal("0.5"))
    assert after == (4010, Decimal("40.5"))
    assert in_a_recent_window == (10, Decimal("0.5")), "history stays in the window it was spent in"


async def test_a_carried_row_is_never_rated_charged_or_queued_for_export(tenant, real_appdata):
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=3.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))
    await carry.run(tenant=tenant)

    async with real_appdata() as conn:
        cur = await conn.execute(
            "SELECT credits, credits_per_usd, markup FROM usage_events WHERE tenant = %s AND event_id LIKE 'ledger:%%'", (tenant,)
        )
        ((credits, rate, markup),) = await cur.fetchall()
        cur = await conn.execute("SELECT count(*) FROM usage_export_outbox WHERE tenant = %s", (tenant,))
        queued = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT count(*) FROM credit_transactions WHERE tenant = %s", (tenant,))
        debited = (await cur.fetchone())[0]

    assert (credits, rate, markup) == (None, None, None)
    assert queued == 0 and debited == 0


async def test_a_tenant_filter_leaves_every_other_tenant_alone(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=1.0)
        await _ledger(conn, other, recorded_at=YEARS_AGO, cost=2.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    await carry.run(tenant=tenant)

    async with real_appdata() as conn:
        assert len(await _carried(conn, tenant)) == 1
        assert await _carried(conn, other) == [], "another tenant's history is only carried when it is asked for"


async def test_a_dry_run_counts_what_it_would_carry_and_writes_nothing(tenant, real_appdata):
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=1.0)
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=2.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    dry = await carry.run(tenant=tenant, dry_run=True)

    assert (dry.carried, dry.carried_usd, dry.dry_run) == (2, Decimal("3.0"), True)
    async with real_appdata() as conn:
        assert await _carried(conn, tenant) == []
    assert (await carry.run(tenant=tenant)).carried == 2, "and a real run then carries exactly what the dry run counted"


async def test_a_run_stopped_at_its_ceiling_is_continued_by_the_next_one(tenant, real_appdata):
    """The statement skips rows already carried. A scan restarted from id 0 would re-read the same first batch forever."""
    async with real_appdata() as conn:
        for i in range(5):
            await _ledger(conn, tenant, recorded_at=YEARS_AGO + timedelta(minutes=i), cost=1.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    runs = [await carry.run(tenant=tenant, batch_size=2, max_batches=1) for _ in range(3)]

    assert [(r.carried, r.complete) for r in runs] == [(2, False), (2, False), (1, True)]
    async with real_appdata() as conn:
        assert len(await _carried(conn, tenant)) == 5


async def test_it_carries_across_batches_in_one_run(tenant, real_appdata):
    async with real_appdata() as conn:
        for i in range(5):
            await _ledger(conn, tenant, recorded_at=YEARS_AGO + timedelta(minutes=i), cost=0.5)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    result = await carry.run(tenant=tenant, batch_size=2)  # 2 + 2 + 1

    assert (result.carried, result.carried_usd, result.complete) == (5, Decimal("2.5"), True)


async def test_with_no_real_event_at_all_the_cutoff_is_now(real_appdata):
    """Asked of an EMPTY table: every real event is deleted inside a transaction that is rolled back, so the shared table
    is untouched. (Only the retention job may delete, and it must say so inside its own transaction.)"""
    async with real_appdata() as conn:
        await conn.execute("SET LOCAL usage_events.allow_delete = 'on'")
        await conn.execute("DELETE FROM usage_events WHERE event_id NOT LIKE 'ledger:%'")

        cutoff = await carry.cutoff_for(conn)
        cur = await conn.execute("SELECT now()")
        (database_now,) = await cur.fetchone()
        await conn.rollback()

    assert cutoff == database_now


async def test_a_carried_row_does_not_move_the_cutoff(tenant, real_appdata):
    """The cutoff is the first REAL event. If a carried row (which is older than it) counted, the second run's cutoff would
    slide back to the oldest history and the run would stop carrying the rest. (Asserted as "not in the history", not as
    "unchanged": other workers seed events too, and a global minimum may legitimately move between two reads.)"""
    async with real_appdata() as conn:
        await _ledger(conn, tenant, recorded_at=YEARS_AGO, cost=1.0)
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))
    await carry.run(tenant=tenant)

    async with real_appdata() as conn:
        cutoff = await carry.cutoff_for(conn)

    assert cutoff > YEARS_AGO + timedelta(days=1), "the carried row, three years old, must not be what the cutoff is"


async def test_two_runs_racing_each_other_carry_every_row_exactly_once(tenant, real_appdata):
    """Both runs can select the same not-yet-carried rows before either has inserted them. `ON CONFLICT DO NOTHING` is what
    turns the loser's duplicate into a no-op instead of a primary-key error; the totals prove nothing was counted twice."""
    async with real_appdata() as conn:
        await conn.execute(
            "INSERT INTO usage_ledger (tenant, principal, thread_id, model_alias, total_tokens, cost_usd, recorded_at) "
            "SELECT %s, 'alice', 't', 'chat', 10, 0.01, %s FROM generate_series(1, 3000)",
            (tenant, YEARS_AGO),
        )
        await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=1))

    first, second = await asyncio.gather(
        carry.run(tenant=tenant, batch_size=3000), carry.run(tenant=tenant, batch_size=3000)
    )

    assert first.carried + second.carried == 3000, "every row once, however the two runs interleaved"
    async with real_appdata() as conn:
        assert len(await _carried(conn, tenant)) == 3000
