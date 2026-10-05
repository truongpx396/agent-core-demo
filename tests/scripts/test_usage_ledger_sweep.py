"""Tests for scripts/usage_ledger_sweep.py — hermetic, same shape as
tests/scripts/test_tool_call_dedup_sweep.py: the delete itself is monkeypatched."""
import asyncio

from app.core.config import USAGE_LEDGER_RETENTION_DAYS
from scripts import usage_ledger_sweep


def test_run_sweep_uses_the_configured_retention_and_returns_the_count(monkeypatch):
    captured = {}

    async def fake_sweep_old_rows(*, older_than_days):
        captured["older_than_days"] = older_than_days
        return 12

    monkeypatch.setattr(usage_ledger_sweep, "sweep_old_rows", fake_sweep_old_rows)

    assert asyncio.run(usage_ledger_sweep.run_sweep()) == 12
    assert captured["older_than_days"] == USAGE_LEDGER_RETENTION_DAYS


def test_run_sweep_honors_a_custom_window(monkeypatch):
    captured = {}

    async def fake_sweep_old_rows(*, older_than_days):
        captured["older_than_days"] = older_than_days
        return 0

    monkeypatch.setattr(usage_ledger_sweep, "sweep_old_rows", fake_sweep_old_rows)

    assert asyncio.run(usage_ledger_sweep.run_sweep(older_than_days=90)) == 0
    assert captured["older_than_days"] == 90
