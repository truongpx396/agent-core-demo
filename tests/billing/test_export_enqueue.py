"""Queuing a usage event for export, in the event's own transaction (app/agent/usage_events.py, specs/010 T023).

Through a fake connection, so this proves WHICH statements run, on which connection, inside which savepoint, and what is
counted when queuing fails. That the outbox row really commits with the event, that a replay queues nothing twice, and
that a tenant with no link gets no row are real-Postgres behaviour: tests/integration/test_usage_export_real_postgres.py.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent import usage_events
from app.billing import providers
from app.core import metrics
from tests.conftest import metric_value

_REAL_INSERT = usage_events._insert  # imported before any autouse fixture patches it

ROW = {
    "event_id": "e1", "tenant": "acme", "principal": "alice", "thread_id": "t", "kind": "chat", "model_alias": "chat",
    "resolved_model": None, "input_tokens": 1, "output_tokens": 2, "cached_input_tokens": 0, "total_tokens": 3,
    "cost_usd": 0.5, "price_input_per_token": 0.1, "price_output_per_token": 0.2,
}


class FakeConnection:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.log: list[tuple] = []
        self.fail_on: str | None = None

    async def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        if self.fail_on and self.fail_on in sql:
            raise ConnectionError("outbox unreachable")
        return SimpleNamespace(rowcount=self.rowcount)

    def transaction(self):
        conn = self

        class Savepoint:
            async def __aenter__(self):
                conn.log.append(("savepoint_begin",))

            async def __aexit__(self, exc_type, exc, tb):
                conn.log.append(("savepoint_end", exc_type.__name__ if exc_type else None))
                return False

        return Savepoint()


@pytest.fixture
def connection(monkeypatch):
    handed_out: list[FakeConnection] = []
    config = SimpleNamespace(rowcount=1, fail_on=None)

    @asynccontextmanager
    async def get_connection():
        conn = FakeConnection(config.rowcount)
        conn.fail_on = config.fail_on
        handed_out.append(conn)
        yield conn

    monkeypatch.setattr(usage_events, "get_connection", get_connection)
    return SimpleNamespace(handed_out=handed_out, config=config)


@pytest.fixture
def exporting(monkeypatch):
    """One enabled provider that bills on usage."""
    monkeypatch.setattr(usage_events, "BILLING_PROVIDERS", ("fake",))
    assert providers.usage_export_providers(("fake",)) == ("fake",)


def _count(path: str) -> float:
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


class TestWhichProvidersExportUsage:
    def test_the_fake_declares_it(self):
        assert providers.usage_export_providers(("fake",)) == ("fake",)

    def test_a_provider_that_is_not_registered_is_not_one(self):
        assert providers.usage_export_providers(("stripe",)) == ()

    def test_nothing_enabled_means_none(self):
        assert providers.usage_export_providers(()) == ()

    def test_a_registered_provider_that_only_sells_credit_packs_is_not_one(self, monkeypatch):
        """PayPal, as far as found, has no usage ingestion: enabling it must not queue usage nobody can send."""

        class WebhookOnly:
            name = "packs"
            capabilities = frozenset()

        monkeypatch.setitem(providers.FACTORIES, "packs", WebhookOnly)

        assert providers.usage_export_providers(("fake", "packs")) == ("fake",)


class TestTheOutboxRowIsPartOfTheEventsTransaction:
    async def test_it_is_queued_on_the_same_connection_inside_a_savepoint(self, connection, exporting):
        assert await _REAL_INSERT(ROW) is True

        assert len(connection.handed_out) == 1, "one connection is one transaction"
        log = connection.handed_out[0].log
        assert [entry[0] for entry in log] == ["execute", "savepoint_begin", "execute", "savepoint_end"]
        _, sql, params = log[2]
        assert "INSERT INTO usage_export_outbox" in sql and "ON CONFLICT (provider, event_id) DO NOTHING" in sql
        assert params == {"event_id": "e1", "tenant": "acme", "providers": ["fake"]}

    async def test_only_a_tenant_with_a_link_gets_a_row_because_the_insert_selects_from_the_links(self, connection, exporting):
        await _REAL_INSERT(ROW)

        sql = connection.handed_out[0].log[2][1]
        assert "FROM billing_customers" in sql and "c.tenant = %(tenant)s" in sql, "a tenant with no link matches no row"

    async def test_a_duplicate_event_queues_nothing(self, connection, exporting):
        connection.config.rowcount = 0

        assert await _REAL_INSERT(ROW) is False

        assert [entry[0] for entry in connection.handed_out[0].log] == ["execute"]

    async def test_with_no_provider_that_bills_on_usage_not_one_extra_statement_runs(self, connection, monkeypatch):
        """SC-005: a deployment that exports nothing pays nothing."""
        monkeypatch.setattr(usage_events, "BILLING_PROVIDERS", ())

        await _REAL_INSERT(ROW)

        assert [entry[0] for entry in connection.handed_out[0].log] == ["execute"]


class TestAFailureToQueueNeverLosesTheEvent:
    async def test_it_is_swallowed_counted_and_the_event_still_stands(self, connection, exporting):
        connection.config.fail_on = "usage_export_outbox"
        before, write_before = _count("export_enqueue"), _count("usage_event_write")

        assert await _REAL_INSERT(ROW) is True, "the event was written; only its queuing failed"

        assert _count("export_enqueue") == before + 1
        assert _count("usage_event_write") == write_before, "this is not a lost event, so it must not page as one"

    async def test_only_the_queuing_is_inside_the_savepoint(self, connection, exporting):
        connection.config.fail_on = "usage_export_outbox"

        await _REAL_INSERT(ROW)

        log = connection.handed_out[0].log
        assert [entry[0] for entry in log][:2] == ["execute", "savepoint_begin"]
        assert log[-1] == ("savepoint_end", "ConnectionError"), "the error reached the savepoint, which rolls back to it"
