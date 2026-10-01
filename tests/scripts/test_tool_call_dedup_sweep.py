"""Tests for scripts/tool_call_dedup_sweep.py — same hermetic-DI approach
as tests/scripts/test_followup_sweep.py/test_ops_digest.py:
sweep_stale_rows is monkeypatched so this never touches a real Postgres.
"""
import asyncio

from scripts import tool_call_dedup_sweep


def test_run_sweep_uses_the_default_retention_window_and_returns_the_count(monkeypatch):
    captured = {}

    async def fake_sweep_stale_rows(*, older_than_hours):
        captured["older_than_hours"] = older_than_hours
        return 5

    monkeypatch.setattr(tool_call_dedup_sweep, "sweep_stale_rows", fake_sweep_stale_rows)

    deleted = asyncio.run(tool_call_dedup_sweep.run_sweep())

    assert deleted == 5
    assert captured["older_than_hours"] == tool_call_dedup_sweep.DEFAULT_RETENTION_HOURS


def test_run_sweep_honors_a_custom_retention_window(monkeypatch):
    captured = {}

    async def fake_sweep_stale_rows(*, older_than_hours):
        captured["older_than_hours"] = older_than_hours
        return 0

    monkeypatch.setattr(tool_call_dedup_sweep, "sweep_stale_rows", fake_sweep_stale_rows)

    deleted = asyncio.run(tool_call_dedup_sweep.run_sweep(older_than_hours=1))

    assert deleted == 0
    assert captured["older_than_hours"] == 1
