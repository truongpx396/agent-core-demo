"""The sales copilot's system of record: leads, their notes and their follow-ups.

Every method takes the tenant and scopes its statement by it (the tenant is the caller's `SecurityCtx`, never the model's). The writes take the
provider's `tool_call_id` where a replay must not duplicate (a lead, a follow-up, a note, a loss), made exactly-once at the ROW level one layer below the
tool-level `idempotent()` claim (constitution Principle IV). A note is its own row, never text appended onto a column. The Postgres adapter is
`app/domains/sales/store.py`; the scheduled follow-up sweep (`scripts/followup_sweep.py`) reads `due_followups` and `mark_followup_done` through it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.ports.slot import Slot


class LeadStore(Protocol):
    async def find_or_create_lead(self, tenant: str, name: str, contact: str, note: str, tool_call_id: str | None = None) -> int: ...

    async def set_lead_status(self, tenant: str, contact: str, status: str) -> bool: ...

    async def get_lead(self, tenant: str, contact: str) -> dict | None: ...

    async def add_followup(
        self, tenant: str, contact: str, due_at: datetime, note: str, created_by: str, tool_call_id: str | None = None
    ) -> int | None: ...

    async def list_pending_followups(self, tenant: str, contact: str | None = None) -> list[dict]: ...

    async def mark_lead_lost(self, tenant: str, contact: str, reason: str, tool_call_id: str | None = None) -> bool: ...

    async def due_followups(self, tenant: str, as_of: datetime) -> list[dict]: ...

    async def mark_followup_done(self, tenant: str, followup_id: int) -> None: ...

    async def append_lead_note(self, tenant: str, contact: str, note: str, tool_call_id: str | None = None) -> bool: ...

    async def lead_history(self, tenant: str, contact: str) -> dict | None: ...


lead_store: Slot[LeadStore] = Slot("lead_store", LeadStore)
