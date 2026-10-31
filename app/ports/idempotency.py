"""The exactly-once ledger for mutating and outward tool calls (constitution Principle IV).

`app/core/tool_idempotency.py` holds the decision (run the tool at most once per `tool_call_id`, fail open when the ledger is down, steer the
agent to verify after a timeout). This is where the claims live (`app/adapters/postgres/tool_idempotency.py`, the `tool_call_dedup` table).

The claim must be ATOMIC at the store: of two callers presenting the same `tool_call_id`, exactly one is told it won. A fake that cannot
promise that is not a fake of this port, which is why the real-Postgres test (tests/integration/test_tool_idempotency_real_postgres.py) is the
proof and the unit tests prove only the consumer's reading of the answer.
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class ToolCallLedger(Protocol):
    async def claim_or_cached(self, tool_call_id: str, tenant: str, thread_id: str | None, tool_name: str) -> str | None:
        """Claims `tool_call_id`. None means THIS caller won (or the earlier claimant's row has no result yet: the narrow race the consumer
        accepts and documents); anything else is the first call's stored result, to hand back unchanged. Raises when the store is unreachable."""
        ...

    async def store_result(self, tool_call_id: str, result: str) -> None:
        """Records the result of the call that won the claim, so a replay can return it."""
        ...

    async def sweep(self, older_than_hours: int) -> int:
        """Deletes claims older than `older_than_hours` and returns how many went. Cron only, never the hot path."""
        ...


tool_call_ledger: Slot[ToolCallLedger] = Slot("tool_call_ledger", ToolCallLedger)
