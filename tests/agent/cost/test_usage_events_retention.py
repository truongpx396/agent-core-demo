"""`app/agent/cost/usage_events_retention.py`: the floor and the control flow of the usage-event retention sweep (specs/010 T030c3).

Hermetic, against a fake `UsageEventStore`: that proves when the loop stops and what it adds up. What is SENT to the database (the delete
permission, that no tenant narrows it) is the adapter's, tested in tests/adapters/postgres/test_usage_events.py; what the database DOES with
the statements (the append-only trigger, the batch counts) is proven against a real Postgres in
tests/integration/test_usage_events_retention_real_postgres.py. (The billing package's own events have their own sweep and tests, since specs/013.)"""
import pytest

from app.agent.cost import usage_events_retention as retention
from app.core.config import USAGE_EVENT_MIN_RETENTION_DAYS
from app.ports import cost as cost_ports
from tests.fakes.cost import InMemoryUsageEventStore


class _Batches(InMemoryUsageEventStore):
    """Answers each `delete_older_than` from a script, and remembers what it was asked."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.asked: list[tuple[int, int]] = []

    async def delete_older_than(self, *, days: int, limit: int) -> int:
        self.asked.append((days, limit))
        return self.script.pop(0)


def _use(script):
    store = _Batches(script)
    return store, cost_ports.usage_events.overriding(store)


async def test_fewer_days_than_the_floor_is_refused_before_the_database_is_touched():
    store, bound = _use([])
    with bound, pytest.raises(ValueError, match="floor"):
        await retention.sweep_old_events(older_than_days=USAGE_EVENT_MIN_RETENTION_DAYS - 1)
    assert store.asked == [], "the floor must be checked before the store is asked anything"


async def test_the_floor_itself_is_allowed():
    _, bound = _use([0])
    with bound:
        result = await retention.sweep_old_events(older_than_days=USAGE_EVENT_MIN_RETENTION_DAYS)

    assert result.complete is True and result.deleted == 0


async def test_the_cutoff_and_the_batch_size_reach_the_store():
    store, bound = _use([0])
    with bound:
        await retention.sweep_old_events(older_than_days=123, batch_size=7)

    assert store.asked == [(123, 7)]


async def test_it_stops_at_the_first_short_batch_and_adds_up_the_batches():
    _, bound = _use([2, 2, 1])
    with bound:
        result = await retention.sweep_old_events(older_than_days=400, batch_size=2)

    assert (result.deleted, result.complete) == (5, True)


async def test_a_run_that_hits_its_ceiling_says_it_is_not_complete():
    _, bound = _use([2, 2])
    with bound:
        result = await retention.sweep_old_events(older_than_days=400, batch_size=2, max_batches=2)

    assert result.deleted == 4 and result.complete is False
