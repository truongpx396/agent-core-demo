"""The sales module's published use cases: what a driving adapter outside it calls (today the scheduled follow-up sweep, `scripts/jobs/followup_sweep.py`).

The tool layer (`tools.py`) is the agent's way in; the sweep is an unattended job that never runs an agent turn, so it reads and closes follow-ups through
the `LeadStore` port (`ports.py`) from here. Both scope by `tenant`, as every method of the port does.
"""
from datetime import datetime

from app.domains.sales.ports import lead_store


async def due_followups(tenant: str, as_of: datetime) -> list[dict]:
    """Pending follow-ups due by `as_of`, each with its lead's name and contact, oldest first."""
    return await lead_store.get().due_followups(tenant, as_of)


async def mark_followup_done(tenant: str, followup_id: int) -> None:
    """Closes a swept follow-up so the next run does not draft it again."""
    await lead_store.get().mark_followup_done(tenant, followup_id)
