"""The team channel: where the agent tells a human something happened.

Support's escalations, sales' handoffs and ops' incidents and digests all end by posting here. It is a saga PIVOT: the write it follows (an
escalation, a handoff, an incident) has already committed and is never rolled back because this send fails, so `post` NEVER RAISES; a failed
sink is counted (`agent_team_channel_notify_total{sink,outcome}`, with an alert) instead. The sinks are the adapter's
(`app/adapters/notify/team_channel.py`: a local file always, Slack when a webhook is configured).
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class TeamChannel(Protocol):
    async def post(self, channel: str, message: str) -> str:
        """Best-effort notify; returns the confirmation text a tool hands back. `channel` is a free-text label ("support-escalations"), not a
        routing parameter."""
        ...


team_channel: Slot[TeamChannel] = Slot("team_channel", TeamChannel)
