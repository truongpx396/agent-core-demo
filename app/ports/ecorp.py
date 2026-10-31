"""The Ecorp domain's data: the knowledge base it writes notes and memories to, and the employee directory it reads.

`KnowledgeBase` stores a note (tenant-wide, a `document`) or a memory (one person's, a `memory`) so that retrieval can find it: the adapter embeds the text
(dense and sparse) and upserts one point (`app/domains/ecorp/store.py`). WHAT is stored and under which id is the domain's
(`app/domains/ecorp/usecases.py`): the point id is derived from the provider's `tool_call_id`, so a replay overwrites the same point instead of adding a
second one (Principle IV), and `tenant`/`owner` come from the caller's `SecurityCtx`, never from the model (Principle I).
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.ports.slot import Slot


class KnowledgeBase(Protocol):
    async def add_note(self, point_id: str, title: str, content: str, topic: str, tenant: str) -> None:
        """Embeds `title: content` and upserts it as a tenant-wide document under `point_id`."""
        ...

    async def add_memory(self, point_id: str, content: str, tenant: str, owner: str, created_at: datetime) -> None:
        """Embeds `content` and upserts it as `owner`'s memory in `tenant` under `point_id`, stamped `created_at` (what the retention filter reads)."""
        ...


class EmployeeDirectory(Protocol):
    async def query_employees(self, tenant: str, department: str | None, name_contains: str | None, limit: int) -> list[dict]:
        """Employees of `tenant`, optionally narrowed by department and name, at most `limit`."""
        ...


knowledge_base: Slot[KnowledgeBase] = Slot("knowledge_base", KnowledgeBase)
employee_directory: Slot[EmployeeDirectory] = Slot("employee_directory", EmployeeDirectory)
