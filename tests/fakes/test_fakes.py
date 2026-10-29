"""The fakes in `tests/fakes/` are interchangeable with the adapters: they match the Protocol, and they behave as the contract says."""
import pytest

from app.ports import cost as cost_ports
from app.ports import models as model_ports
from app.ports.cost import Override
from tests.fakes.conformance import mismatches
from tests.fakes.cost import (
    InMemoryBudgetHoldStore,
    InMemoryBudgetPolicyStore,
    InMemoryUsageEventStore,
    NoGatewayModelCatalog,
)

FAKES = [
    (cost_ports.usage_events, InMemoryUsageEventStore),
    (cost_ports.budget_holds, InMemoryBudgetHoldStore),
    (cost_ports.budget_policies, InMemoryBudgetPolicyStore),
    (model_ports.model_catalog, NoGatewayModelCatalog),
]


@pytest.mark.parametrize(("slot", "fake"), FAKES, ids=[slot.name for slot, _ in FAKES])
def test_a_fake_conforms_to_the_protocol_it_stands_in_for(slot, fake):
    assert mismatches(slot.port, fake()) == []


ROW = {
    "event_id": "e1", "tenant": "acme", "principal": "alice", "thread_id": "t", "kind": "chat", "model_alias": "chat",
    "resolved_model": None, "input_tokens": 1, "output_tokens": 2, "cached_input_tokens": 0, "total_tokens": 3,
    "cost_usd": 0.5, "price_input_per_token": None, "price_output_per_token": None,
}


async def test_the_usage_fake_is_idempotent_on_the_event_id_and_scopes_totals_by_tenant():
    store = InMemoryUsageEventStore()

    assert await store.insert(dict(ROW)) is True  # type: ignore[arg-type] - a TypedDict literal
    assert await store.insert(dict(ROW)) is False
    await store.insert(dict(ROW, event_id="e2", tenant="other"))  # type: ignore[arg-type]

    assert (await store.totals("acme")).total_tokens == 3
    assert await store.known_tenants() == {"acme", "other"}


async def test_the_hold_fake_releases_only_its_own_hold_for_its_own_tenant():
    holds = InMemoryBudgetHoldStore()
    await holds.reserve("acme", "h1", 1.0, stale_after_minutes=5)
    await holds.reserve("other", "h2", 2.0, stale_after_minutes=5)

    await holds.release("other", "h1")  # a hold id under the wrong tenant is a no-op
    assert await holds.in_flight("acme", stale_after_minutes=5) == 1.0
    await holds.release("acme", "h1")
    assert await holds.in_flight("acme", stale_after_minutes=5) == 0.0


async def test_the_policy_fake_returns_the_tenants_own_the_default_and_the_persons_but_not_another_persons():
    policies = InMemoryBudgetPolicyStore()
    await policies.upsert("acme", "", "day", 10.0, "op")
    await policies.upsert("acme", "*", "day", 2.0, "op")
    await policies.upsert("acme", "alice", "day", 5.0, "op")
    await policies.upsert("acme", "bob", "day", 9.0, "op")

    got = {(o.subject, o.period): o.limit_usd for o in await policies.for_person("acme", "alice")}

    assert got == {("", "day"): 10.0, ("*", "day"): 2.0, ("alice", "day"): 5.0}
    assert await policies.delete("acme", "bob", "day") is True
    assert await policies.delete("acme", "bob", "day") is False
    assert Override("a", "day", None).limit_usd is None
