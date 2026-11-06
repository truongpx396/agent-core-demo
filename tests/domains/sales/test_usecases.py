"""The sales module's published use cases (`app/domains/sales/usecases.py`): the follow-up sweep reaches the lead table through the `LeadStore` port, scoped by tenant."""
from datetime import UTC, datetime

from app.domains.sales import usecases
from app.domains.sales.ports import lead_store


class _Leads:
    def __init__(self):
        self.calls = []

    async def due_followups(self, tenant, as_of):
        self.calls.append(("due", tenant, as_of))
        return [{"id": 3, "lead_name": "Ana", "contact": "ana@x.io", "note": "call"}]

    async def mark_followup_done(self, tenant, followup_id):
        self.calls.append(("done", tenant, followup_id))


async def test_due_followups_asks_the_port_for_this_tenant_as_of_the_given_time():
    leads, now = _Leads(), datetime(2026, 11, 6, tzinfo=UTC)
    with lead_store.overriding(leads):
        due = await usecases.due_followups("acme", now)

    assert due == [{"id": 3, "lead_name": "Ana", "contact": "ana@x.io", "note": "call"}]
    assert leads.calls == [("due", "acme", now)]


async def test_marking_a_followup_done_goes_through_the_port_for_this_tenant():
    leads = _Leads()
    with lead_store.overriding(leads):
        await usecases.mark_followup_done("acme", 3)

    assert leads.calls == [("done", "acme", 3)]
