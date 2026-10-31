"""Points the Postgres adapters (`app/adapters/postgres/`) at a real database for one test, and binds them to their ports.

The hermetic suite binds fakes for the usage-event port and guards `get_connection` in every adapter (tests/conftest.py), so an integration
test that wants the real path has to undo both. One helper does it, so a new adapter is added in one place."""
from app.adapters.postgres import budget_holds, budget_policies, usage_events
from app.ports import cost as cost_ports


def use_real_postgres(monkeypatch, get_connection) -> None:
    """`get_connection` is the test's own factory for a connection to the container."""
    for module in (usage_events, budget_holds, budget_policies):
        monkeypatch.setattr(module, "get_connection", get_connection)
    # `Slot.get` shadowed on the instance: monkeypatch restores it, and the application keeps asking the port, as in production.
    monkeypatch.setattr(cost_ports.usage_events, "get", lambda: usage_events.PgUsageEventStore())
    monkeypatch.setattr(cost_ports.budget_holds, "get", lambda: budget_holds.PgBudgetHoldStore())
    monkeypatch.setattr(cost_ports.budget_policies, "get", lambda: budget_policies.PgBudgetPolicyStore())
