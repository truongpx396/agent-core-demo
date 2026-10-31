"""The session directory: which conversation threads exist and who owns them.

The web UI's switcher, the ownership gate on every message and the checks behind `GET /chat/sessions/{id}/messages` all read it. The rules
(a valid context is required, a title is cut to length, a bookkeeping write never fails a turn while an ownership check never defaults to
"yes") are the application's (`app/agent/runtime/sessions.py`); this is only where the rows live (`app/adapters/postgres/sessions.py`).

Every row is scoped by tenant, principal and domain, and every method takes all three: a thread opened under another tenant, person or
domain reads as not found. Methods raise when the store cannot be reached; the application decides what that means.
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class SessionStore(Protocol):
    async def touch(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> None:
        """Records the thread, or refreshes its `last_active_at` when it exists. `title` and `domain` are written on first insert only."""
        ...

    async def claim(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> bool:
        """Atomically creates the thread if nobody has it, then reports whether this tenant, person and domain own it."""
        ...

    async def owns(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        """Whether the thread exists and belongs to exactly this tenant, person and domain. Does not create it."""
        ...

    async def list(self, tenant: str, principal: str, domain: str) -> list[dict]:
        """The person's threads in the domain, most recently active first: `thread_id`, `title`, `created_at`, `last_active_at`."""
        ...


session_store: Slot[SessionStore] = Slot("session_store", SessionStore)
