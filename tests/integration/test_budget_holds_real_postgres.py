"""The in-flight budget reservation against a REAL Postgres.

tests/agent/test_usage_ledger.py pins the SQL the ledger sends (tenant in every
WHERE, the staleness cutoff). What a fake cursor cannot show is how the real
table behaves over TIME — which is exactly where this feature failed, twice,
before it moved from one running total per tenant to one hold per turn
(postgres-init/16-tenant-budget-holds.sql; spec 008 B20):

  * a worker killed mid-turn never releases its amount. The read ignored it
    after five minutes, but the NEXT reserve added to the stale amount, so the
    dead turn's spend came back as if it were in flight and never left;
  * while a tenant kept running turns, every reserve/release refreshed the one
    shared `updated_at`, so a leaked amount never aged out at all.

Both were reproduced against this same table design before the fix. "Time
passing" is simulated by moving a hold's `created_at` into the past — the same
thing five real minutes do — rather than by sleeping.

Self-provisioned via `tests/containers.py::ensure_postgres()` (applies the real
`postgres-init/*.sql`, so the schema under test is the shipped one; skips
cleanly without Docker). Each test uses its own unique tenant — the container
is shared across tests and xdist workers.
"""
import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest

from app.agent import usage_ledger
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

_STALE = usage_ledger.RESERVATION_STALE_AFTER_MINUTES


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    """Point the ledger at the real database. It is the module's OWN
    `get_connection` binding that has to be replaced (tests/conftest.py's
    autouse `mock_appdata_postgres` patches the same name for every other test)."""

    @asynccontextmanager
    async def get_connection():
        # A psycopg connection used as `async with` commits on a clean exit —
        # the same contract sql_store.get_connection documents.
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    monkeypatch.setattr(usage_ledger, "get_connection", get_connection)
    return get_connection


@pytest.fixture
def tenant() -> str:
    return f"acme-{uuid.uuid4().hex[:8]}"


def _ctx(tenant: str) -> dict:
    return {"tenant": tenant, "principal": "alice", "claims": {}}


async def _age(real_appdata, tenant: str, minutes: int) -> None:
    async with real_appdata() as conn:
        await conn.execute(
            "UPDATE tenant_budget_holds SET created_at = created_at - make_interval(mins => %s) "
            "WHERE tenant = %s",
            (minutes, tenant),
        )


async def _hold_count(real_appdata, tenant: str) -> int:
    async with real_appdata() as conn:
        cur = await conn.execute("SELECT count(*) FROM tenant_budget_holds WHERE tenant = %s", (tenant,))
        return (await cur.fetchone())[0]


async def test_a_turns_hold_counts_while_it_runs_and_is_gone_after_its_release(tenant):
    hold = await usage_ledger.reserve_budget(_ctx(tenant), 0.5)

    assert hold is not None
    assert await usage_ledger.in_flight_reservation(tenant) == 0.5

    await usage_ledger.release_budget_reservation(_ctx(tenant), hold)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.0


async def test_concurrent_turns_add_up(tenant):
    await usage_ledger.reserve_budget(_ctx(tenant), 0.5)
    await usage_ledger.reserve_budget(_ctx(tenant), 0.25)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.75


async def test_an_abandoned_hold_is_not_resurrected_by_the_next_reserve(tenant, real_appdata):
    """The spec's Scenario B20. A worker dies after reserving and never
    releases; ten minutes on it no longer counts; a new turn then reserves —
    and the read must show ONLY that turn, not the dead one back as well."""
    await usage_ledger.reserve_budget(_ctx(tenant), 0.5)  # the turn whose worker was killed
    await _age(real_appdata, tenant, minutes=_STALE + 5)
    assert await usage_ledger.in_flight_reservation(tenant) == 0.0, "control: an abandoned hold is ignored"

    healthy = await usage_ledger.reserve_budget(_ctx(tenant), 0.5)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.5  # was 1.0 with the running total
    await usage_ledger.release_budget_reservation(_ctx(tenant), healthy)
    assert await usage_ledger.in_flight_reservation(tenant) == 0.0  # was 0.5: the leak outlived every release


async def test_an_abandoned_hold_ages_out_even_while_the_tenant_keeps_working(tenant, real_appdata):
    """The case a one-statement "reset a stale row on reserve" fix would still
    miss: with one timestamp per tenant, every healthy turn's reserve/release
    refreshes it, so a tenant that is never idle for five minutes carries a
    leak forever. Here a healthy turn runs every four (simulated) minutes for
    forty, and the leak must stop counting on its own clock."""
    await usage_ledger.reserve_budget(_ctx(tenant), 0.5)  # leaked
    for _ in range(10):
        await _age(real_appdata, tenant, minutes=4)
        healthy = await usage_ledger.reserve_budget(_ctx(tenant), 0.1)
        await usage_ledger.release_budget_reservation(_ctx(tenant), healthy)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.0


async def test_reserve_sweeps_this_tenants_abandoned_holds_and_only_this_tenants(tenant, real_appdata):
    other = f"globex-{uuid.uuid4().hex[:8]}"
    await usage_ledger.reserve_budget(_ctx(tenant), 0.5)
    await usage_ledger.reserve_budget(_ctx(other), 0.5)
    await _age(real_appdata, tenant, minutes=_STALE + 5)
    await _age(real_appdata, other, minutes=_STALE + 5)

    await usage_ledger.reserve_budget(_ctx(tenant), 0.25)

    assert await _hold_count(real_appdata, tenant) == 1, "the abandoned hold was swept, the new one kept"
    assert await _hold_count(real_appdata, other) == 1, "another tenant's rows are not this tenant's to delete"


async def test_a_second_release_is_a_no_op_and_cannot_reach_another_turns_hold(tenant):
    first = await usage_ledger.reserve_budget(_ctx(tenant), 0.5)
    await usage_ledger.reserve_budget(_ctx(tenant), 0.25)

    await usage_ledger.release_budget_reservation(_ctx(tenant), first)
    await usage_ledger.release_budget_reservation(_ctx(tenant), first)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.25


async def test_holds_are_tenant_scoped_for_the_read_and_for_release(tenant):
    """Principle I: another tenant neither sees this tenant's holds nor, even
    knowing a hold id, can release it."""
    other = f"globex-{uuid.uuid4().hex[:8]}"
    hold = await usage_ledger.reserve_budget(_ctx(tenant), 0.5)

    assert await usage_ledger.in_flight_reservation(other) == 0.0

    await usage_ledger.release_budget_reservation(_ctx(other), hold)

    assert await usage_ledger.in_flight_reservation(tenant) == 0.5
