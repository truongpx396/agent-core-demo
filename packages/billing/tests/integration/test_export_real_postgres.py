"""Usage export against a REAL Postgres (sql/24-billing-usage-events.sql and 25-billing-export-outbox.sql; credit_billing/export.py; moved from the agent
app with the code by specs/013): each usage event reaches a billing provider exactly once, or fails loudly.

Every guarantee here is database behaviour, so a fake would only echo back what was written (constitution VII: reliance on a real constraint,
lock or transaction is stated and tested at this tier, not assumed):

  * `FOR UPDATE SKIP LOCKED` means two workers running at once take disjoint batches: no event is sent twice;
  * a crash between "the provider accepted it" and "marked sent" re-sends the event, and the provider's idempotency key (the
    event id) is what makes that harmless: the one window the database cannot see, shown to be survivable;
  * an event past the age limit is `expired`, counted, and never sent: given up on BEFORE the provider's own window closes;
  * the rows are guarded by the schema: finished is terminal, identity is immutable, and an event cannot be exported under another tenant.

(That the outbox row commits WITH its usage event, only for a tenant linked to a provider that bills on usage, and that a replay queues nothing
twice, is tests/integration/test_usage_real_postgres.py: the recording is `credit_billing/usage.py`'s.)

Each test uses its own tenant, customer AND PROVIDER NAME (the outbox is shared across tests and the worker drains every due
row of a provider, so a shared name would let one test send another's rows).
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors as pg_errors

from credit_billing import export, metrics, store
from credit_billing.providers.base import ExportResult
from credit_billing.providers.fake import FakeProvider
from tests.conftest import metric_value
from tests.containers import ensure_postgres
from tests.integration.schema_reapply import reapply

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    monkeypatch.setattr(export, "get_connection", get_connection)


class World:
    """One test's tenant, customer and provider name: unique, so tests never touch each other's rows."""

    def __init__(self, url: str):
        self.url = url
        tag = uuid.uuid4().hex[:10]
        self.tenant, self.customer, self.provider = f"t-{tag}", f"cus_{tag}", f"p-{tag}"
        self.fake = FakeProvider("secret")
        self.adapters = {self.provider: self.fake}

    async def link(self) -> None:
        async with await psycopg.AsyncConnection.connect(self.url) as conn:
            await store.link_customer(conn, self.tenant, self.provider, self.customer)

    async def event(self, *, age_days: int = 0, tenant: str | None = None) -> str:
        """A usage event written directly (so its age can be set); the real write path is tests/integration/test_usage_real_postgres.py."""
        event_id = f"ev-{uuid.uuid4().hex[:12]}"
        async with await psycopg.AsyncConnection.connect(self.url) as conn:
            await conn.execute(
                "INSERT INTO billing_usage_events (tenant, event_id, kind, model, tokens, cost_usd, credits, credits_per_usd, markup, occurred_at) "
                "VALUES (%s, %s, 'chat', 'chat', 150, 0.01, 10, 1000, 1, now() - make_interval(days => %s))",
                (tenant or self.tenant, event_id, age_days),
            )
        return event_id

    async def queue(self, event_id: str, *, tenant: str | None = None, created_days_ago: int = 0) -> None:
        async with await psycopg.AsyncConnection.connect(self.url) as conn:
            await conn.execute(
                "INSERT INTO billing_export_outbox (provider, tenant, event_id, created_at) VALUES (%s, %s, %s, now() - make_interval(days => %s))",
                (self.provider, tenant or self.tenant, event_id, created_days_ago),
            )

    async def ready(self, n: int = 1, **kwargs) -> list[str]:
        """`n` linked, queued, due events."""
        await self.link()
        ids = [await self.event(**kwargs) for _ in range(n)]
        for event_id in ids:
            await self.queue(event_id)
        return ids

    async def rows(self) -> dict[str, tuple]:
        async with await psycopg.AsyncConnection.connect(self.url) as conn:
            cur = await conn.execute(
                "SELECT event_id, status, attempts, last_error_class, sent_at, next_attempt_at FROM billing_export_outbox WHERE provider = %s",
                (self.provider,),
            )
            return {row[0]: row[1:] for row in await cur.fetchall()}

    async def run(self, **kwargs) -> dict[str, int]:
        kwargs.setdefault("base", 0)  # no waiting between attempts unless a test is about waiting
        kwargs.setdefault("cap", 0)
        return await export.run_once(self.adapters, **kwargs)

    def count(self, outcome: str) -> float:
        return metric_value(metrics.agent_usage_export_total, provider=self.provider, outcome=outcome)


@pytest.fixture
def world(appdata_url) -> World:
    return World(appdata_url)


class TestOneDeliveryHoweverManyAttempts:
    async def test_it_fails_twice_then_succeeds_and_the_provider_has_it_once(self, world):
        (event_id,) = await world.ready()
        world.fake.script("retryable", "retryable")

        first, second, third = await world.run(), await world.run(), await world.run()

        assert (first, second, third) == ({"retry": 1}, {"retry": 1}, {"sent": 1})
        status, attempts, error, sent_at, _ = (await world.rows())[event_id]
        assert (status, attempts, error) == ("sent", 3, None) and sent_at is not None
        assert world.fake.received.keys() == {event_id}, "one delivery, however many attempts"
        assert world.fake.calls == [[event_id]] * 3
        assert (world.count("retry"), world.count("sent")) == (2, 1)

    async def test_a_retry_waits_out_its_backoff_before_it_is_sent_again(self, world):
        (event_id,) = await world.ready()
        world.fake.script("retryable")

        await world.run(base=3600, cap=3600)
        again = await world.run(base=3600, cap=3600)  # the next attempt is an hour away

        assert again == {} and world.fake.calls == [[event_id]], "not due, so not sent"
        _, attempts, _, _, next_attempt = (await world.rows())[event_id]
        assert attempts == 1 and next_attempt is not None

    async def test_two_workers_running_at_once_take_disjoint_batches_so_nothing_is_sent_twice(self, world):
        """`FOR UPDATE SKIP LOCKED`: the second worker does not wait for, or repeat, the first's batch."""
        ids = await world.ready(10)
        world.fake.delay = 0.3  # holds the first batch in flight while the second worker looks

        await asyncio.gather(world.run(batch_size=5), world.run(batch_size=5))

        (a_start, a_end), (b_start, b_end) = sorted(world.fake.spans)
        assert b_start < a_end, "the second worker was sending while the first still was: it skipped the locked rows, it did not wait for them"
        assert len(world.fake.calls) == 2 and all(len(call) == 5 for call in world.fake.calls)
        assert not set(world.fake.calls[0]) & set(world.fake.calls[1]), "no event in both batches"
        assert set(world.fake.calls[0]) | set(world.fake.calls[1]) == set(ids)
        assert {row[0] for row in (await world.rows()).values()} == {"sent"}
        assert sorted(world.fake.received) == sorted(ids)

    async def test_a_crash_between_sent_and_marked_sent_resends_and_the_provider_dedupes(self, world, monkeypatch):
        """The one window the database cannot see (module docstring): the provider accepted it, then the worker died before
        recording that. The row is pending again, so it is sent again, and the event id makes the second send a no-op."""
        (event_id,) = await world.ready()
        real_record = export._record

        async def die(*args, **kwargs):
            raise RuntimeError("the worker died before it could record the send")

        monkeypatch.setattr(export, "_record", die)
        with pytest.raises(RuntimeError):
            await world.run()
        assert world.fake.received.keys() == {event_id}, "the provider has it"
        assert (await world.rows())[event_id][0] == "pending", "but the outbox does not know"

        monkeypatch.setattr(export, "_record", real_record)
        assert await world.run() == {"sent": 1}

        assert (await world.rows())[event_id][0] == "sent"
        assert list(world.fake.received) == [event_id], "recorded (and so billed) once, though it was sent twice"
        assert world.fake.calls == [[event_id], [event_id]]


class TestGivingUpLoudly:
    async def test_an_event_past_the_age_limit_is_expired_counted_and_never_sent(self, world):
        """Stripe rejects events older than 35 days: retrying past that is a silent discard, so it ends first, with a count."""
        await world.link()
        old, fresh = await world.event(age_days=31), await world.event(age_days=29)
        await world.queue(old)
        await world.queue(fresh)

        totals = await world.run(max_age_days=30)

        assert totals == {"expired": 1, "sent": 1}
        rows = await world.rows()
        assert rows[old][0] == "expired" and rows[old][2] == "max_age"
        assert rows[fresh][0] == "sent"
        assert world.fake.calls == [[fresh]], "the expired event was never sent"
        assert world.count("expired") == 1, "this is what pages (UsageExportExpired)"

    async def test_an_expired_event_stays_expired(self, world):
        await world.link()
        old = await world.event(age_days=40)
        await world.queue(old)
        await world.run(max_age_days=30)

        assert await world.run(max_age_days=30) == {} and world.fake.calls == []

    async def test_a_permanent_refusal_is_failed_at_once_and_never_retried(self, world):
        (event_id,) = await world.ready()
        world.fake.script("permanent")

        assert await world.run() == {"failed": 1}
        assert await world.run() == {}, "a failed event is not retried"

        status, attempts, error, _, _ = (await world.rows())[event_id]
        assert (status, attempts, error) == ("failed", 1, "provider_permanent")
        assert world.count("failed") == 1, "this is what pages (UsageExportFailed)"

    async def test_the_attempt_budget_ends_the_retrying(self, world):
        (event_id,) = await world.ready()
        world.fake.script("retryable", "retryable", "retryable")

        totals = [await world.run(max_attempts=3) for _ in range(4)]

        assert totals == [{"retry": 1}, {"retry": 1}, {"failed": 1}, {}]
        assert (await world.rows())[event_id][1:3] == (3, "attempts_exhausted:provider_retryable")

    async def test_an_event_whose_customer_link_is_gone_is_failed_not_retried_for_a_month(self, world, appdata_url):
        (event_id,) = await world.ready()
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("DELETE FROM billing_customers WHERE tenant = %s AND provider = %s", (world.tenant, world.provider))

        assert await world.run() == {"failed": 1}

        assert (await world.rows())[event_id][2] == "unlinked_customer" and world.fake.calls == [], "nothing was sent for a customer we cannot name"

    async def test_a_provider_that_raises_is_a_retry_recorded_by_class_name_only(self, world):
        (event_id,) = await world.ready()
        world.fake.script("raise")

        assert await world.run() == {"retry": 1}

        status, _, error, _, _ = (await world.rows())[event_id]
        assert (status, error) == ("pending", "ConnectionError")

    async def test_a_provider_that_hangs_is_cut_off_at_the_deadline_and_retried(self, world):
        """The worker holds the batch's row locks while it waits, so the wait has a ceiling."""
        (event_id,) = await world.ready()
        world.fake.delay = 5

        assert await world.run(call_timeout=0.05) == {"retry": 1}

        assert (await world.rows())[event_id][2] == "TimeoutError"

    async def test_an_event_the_adapter_never_mentions_is_not_assumed_sent(self, world):
        (event_id,) = await world.ready()

        class Forgetful:
            name = "forgetful"

            async def export_usage(self, customer, events):
                return ExportResult()

        world.adapters = {world.provider: Forgetful()}

        assert await world.run() == {"retry": 1}
        assert (await world.rows())[event_id][1:3] == (1, "unreported")

    async def test_the_oldest_waiting_event_is_published_as_a_gauge(self, world):
        await world.link()
        old = await world.event()
        await world.queue(old, created_days_ago=3)
        world.fake.script("retryable")

        await world.run(base=3600, cap=3600)

        age = metric_value(metrics.agent_usage_export_oldest_pending_age_seconds, provider=world.provider)
        assert 3 * 86400 <= age < 4 * 86400

    async def test_the_gauge_reads_zero_when_nothing_is_waiting(self, world):
        await world.ready()

        await world.run()

        assert metric_value(metrics.agent_usage_export_oldest_pending_age_seconds, provider=world.provider) == 0


class TestWhatIsSent:
    async def test_the_provider_is_given_the_events_own_figures_under_the_customers_reference(self, world):
        (event_id,) = await world.ready()

        await world.run()

        customer_ref, usage = world.fake.received[event_id]
        assert customer_ref == world.customer
        assert (usage.event_id, usage.credits, usage.cost_usd, usage.model, usage.total_tokens) == (event_id, Decimal("10"), Decimal("0.01"), "chat", 150)

    async def test_one_tenants_events_are_never_sent_under_another_tenants_customer(self, world, appdata_url):
        other = World(appdata_url)
        other.provider = world.provider  # the same provider, two tenants
        other.adapters = world.adapters
        (mine,) = await world.ready()
        await other.link()
        theirs = await other.event()
        await other.queue(theirs)

        await world.run()

        assert world.fake.received[mine][0] == world.customer and world.fake.received[theirs][0] == other.customer


class TestTheSchemaGuards:
    async def test_a_sent_row_is_terminal(self, world, appdata_url):
        (event_id,) = await world.ready()
        await world.run()

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException, match="finished"):
                await conn.execute("UPDATE billing_export_outbox SET status = 'pending', sent_at = NULL WHERE provider = %s AND event_id = %s", (world.provider, event_id))

    async def test_what_a_row_is_never_changes(self, world, appdata_url):
        (event_id,) = await world.ready()

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException, match="never changes"):
                await conn.execute("UPDATE billing_export_outbox SET tenant = 'someone-else' WHERE provider = %s AND event_id = %s", (world.provider, event_id))

    async def test_an_event_cannot_be_queued_under_another_tenant(self, world):
        """A bug cannot export one tenant's usage under another's name: the foreign key is on (tenant, event_id), so there is no such event."""
        await world.link()
        mine = await world.event()

        with pytest.raises(pg_errors.ForeignKeyViolation):
            await world.queue(mine, tenant="someone-else")

    async def test_the_same_event_is_queued_once_per_provider(self, world):
        (event_id,) = await world.ready()

        with pytest.raises(pg_errors.UniqueViolation):
            await world.queue(event_id)

    async def test_the_script_can_be_applied_twice(self, world, appdata_url):
        await reapply(appdata_url, "25-billing-export-outbox.sql")  # no exception (see tests/integration/schema_reapply.py)
