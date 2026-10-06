"""`usage_events` against a REAL Postgres (postgres-init/19-usage-events.sql).

tests/agent/test_usage_events.py proves the statement's text and every failure path through a fake
connection. A fake cannot show what the TABLE does, and the whole design leans on four things that
only a real database enforces (constitution VII: reliance on a real UNIQUE / ON CONFLICT is stated
and tested at this tier, not assumed):

  * `event_id` is a PRIMARY KEY, so writing the same call twice, even concurrently, is one row. This
    is the duplicate story that makes a replay, a retried export and a provider's own
    idempotency key all agree on what one call is;
  * rows are append-only: an UPDATE is refused, a DELETE is refused unless the retention job says so
    inside its own transaction (and cannot leave that switched on);
  * NULL cost ("unpriced") and 0 ("free") stay different things;
  * the CHECK constraints refuse a nonsense kind or negative figure from anyone who bypasses Python.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from psycopg import errors as pg_errors

from app.agent import metering, usage_events
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

_REAL_INSERT = usage_events._insert


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    monkeypatch.setattr(usage_events, "get_connection", get_connection)
    monkeypatch.setattr(usage_events, "_insert", _REAL_INSERT)  # the autouse sink replaced it


def _row(tenant: str, **overrides) -> dict:
    row = {
        "event_id": str(uuid.uuid4()), "tenant": tenant, "principal": "alice", "thread_id": "t",
        "kind": "chat", "model_alias": "chat", "resolved_model": None, "input_tokens": 100,
        "output_tokens": 50, "cached_input_tokens": 0, "total_tokens": 150, "cost_usd": 0.25,
        "price_input_per_token": 0.001, "price_output_per_token": 0.002,
    }
    row.update(overrides)
    return row


async def _rows(url: str, tenant: str) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(
            "SELECT event_id, cost_usd, kind FROM usage_events WHERE tenant = %s ORDER BY recorded_at", (tenant,)
        )
        return await cur.fetchall()


async def test_the_same_event_written_twice_is_one_row(appdata_url):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    row = _row(tenant)

    assert await _REAL_INSERT(row) is True
    assert await _REAL_INSERT(row) is False

    assert len(await _rows(appdata_url, tenant)) == 1


async def test_twenty_concurrent_writers_of_the_same_event_produce_exactly_one_row(appdata_url):
    """The race a replayed turn and a retried worker would create: the primary key arbitrates."""
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    row = _row(tenant)

    results = await asyncio.gather(*[_REAL_INSERT(row) for _ in range(20)])

    assert results.count(True) == 1
    assert len(await _rows(appdata_url, tenant)) == 1


async def test_an_unpriced_call_is_null_and_a_free_call_is_zero(appdata_url):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant, cost_usd=None, price_input_per_token=None, price_output_per_token=None))
    await _REAL_INSERT(_row(tenant, cost_usd=0.0))

    costs = sorted((r[1] for r in await _rows(appdata_url, tenant)), key=lambda c: (c is not None, c))

    assert costs[0] is None, "unknown must stay unknown"
    assert float(costs[1]) == 0.0, "and free must stay free"


async def test_money_is_stored_as_numeric_not_float(appdata_url):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant, cost_usd=0.0123456))

    ((_, cost, _),) = await _rows(appdata_url, tenant)

    assert str(cost) == "0.012345600000", "NUMERIC keeps what was written; a float column would drift"


async def test_a_call_costing_a_fraction_of_a_millionth_of_a_dollar_is_not_stored_as_zero(appdata_url):
    """A cheap model or an embedding: $0.0000004. At six decimal places that is exactly zero, which for
    billing is a systematic loss on every such call, so the column carries twelve."""
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant, cost_usd=0.0000004))

    ((_, cost, _),) = await _rows(appdata_url, tenant)

    assert cost == Decimal("0.0000004") and cost != 0


async def test_an_update_is_always_refused(appdata_url):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant))

    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        with pytest.raises(pg_errors.RaiseException, match="append-only"):
            await conn.execute("UPDATE usage_events SET cost_usd = 0 WHERE tenant = %s", (tenant,))


async def test_a_delete_is_refused_unless_the_retention_job_says_so_for_its_own_transaction(appdata_url):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant))

    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        with pytest.raises(pg_errors.RaiseException, match="append-only"):
            await conn.execute("DELETE FROM usage_events WHERE tenant = %s", (tenant,))
    assert len(await _rows(appdata_url, tenant)) == 1

    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        await conn.execute("SET LOCAL usage_events.allow_delete = 'on'")
        await conn.execute("DELETE FROM usage_events WHERE tenant = %s", (tenant,))
    assert await _rows(appdata_url, tenant) == []


async def test_the_delete_permission_does_not_outlive_its_transaction(appdata_url):
    """SET LOCAL ends with the transaction, so the switch cannot be left on: on the SAME
    connection, after the commit that ends the transaction it was set in, a delete is refused again."""
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _REAL_INSERT(_row(tenant))

    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        await conn.execute("SET LOCAL usage_events.allow_delete = 'on'")
        await conn.commit()
        with pytest.raises(pg_errors.RaiseException, match="append-only"):
            await conn.execute("DELETE FROM usage_events WHERE tenant = %s", (tenant,))
    assert len(await _rows(appdata_url, tenant)) == 1


@pytest.mark.parametrize(
    "bad",
    [{"kind": "chta"}, {"input_tokens": -1}, {"total_tokens": -5}, {"cost_usd": -0.01}],
    ids=["unknown-kind", "negative-input", "negative-total", "negative-cost"],
)
async def test_the_check_constraints_refuse_nonsense_even_from_a_caller_that_bypasses_python(bad):
    with pytest.raises(pg_errors.CheckViolation):
        await _REAL_INSERT(_row(f"t-{uuid.uuid4().hex[:8]}", **bad))


async def test_the_tenant_time_index_exists(appdata_url):
    async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
        cur = await conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'usage_events_tenant_occurred_idx'")
        (definition,) = await cur.fetchone()

    assert "(tenant, occurred_at)" in definition


async def test_a_metered_call_lands_as_one_row_and_a_replay_of_it_adds_nothing(appdata_url):
    """The whole path with a real table: a call goes through the choke point and is written; the
    same response written again (a replayed turn) is recognised by its derived id."""
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    ctx = {"tenant": tenant, "principal": "alice", "claims": {}}
    config = {"configurable": {"ctx": ctx, "thread_id": "thread-1"}}
    reply = AIMessage(content="an answer", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    call = await metering.metered_invoke(
        GenericFakeChatModel(messages=iter([reply])), [HumanMessage(content="q")],
        config=config, kind="chat", model_alias="chat",
    )
    assert len(await _rows(appdata_url, tenant)) == 1

    await usage_events.record_call(
        ctx, thread_id="thread-1", message_id=call.response.id, kind="chat", model_alias="chat", priced=call.priced
    )

    ((event_id, _, kind),) = await _rows(appdata_url, tenant)
    assert event_id == usage_events.event_id_for(tenant, call.response.id)
    assert kind == "chat"
