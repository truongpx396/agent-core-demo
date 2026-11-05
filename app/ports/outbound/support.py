"""The support copilot's system of record: tickets and their comments.

Every method takes the tenant and scopes its statement by it; the tenant comes from the caller's `SecurityCtx`, never from the model. The two writes
(`create_ticket`, `add_comment`) take the provider's `tool_call_id` and the store makes them exactly-once at the ROW level (`tool_call_id UNIQUE` +
`ON CONFLICT DO NOTHING`), one layer below the tool-level `idempotent()` claim (constitution Principle IV). A comment is its own row, never text
appended onto a column. The Postgres adapter is `app/domains/support/store.py`.
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class TicketStore(Protocol):
    async def create_ticket(
        self, tenant: str, requester: str, subject: str, description: str, priority: str, tool_call_id: str | None = None
    ) -> int:
        """Inserts a new `open` ticket and returns its id (fresh, never caller-targeted). With a `tool_call_id`, a replay returns the ticket the first call made."""
        ...

    async def get_ticket(self, tenant: str, ticket_id: int) -> dict | None: ...

    async def list_tickets_for_requester(self, tenant: str, requester: str, limit: int = 10) -> list[dict]: ...

    async def escalate_ticket(self, tenant: str, ticket_id: int, reason: str) -> bool:
        """True if the ticket exists in this tenant and was escalated."""
        ...

    async def add_comment(self, tenant: str, ticket_id: int, comment: str, tool_call_id: str | None = None) -> bool:
        """True if the ticket exists in this tenant and the comment was recorded (once per `tool_call_id`)."""
        ...


ticket_store: Slot[TicketStore] = Slot("ticket_store", TicketStore)
