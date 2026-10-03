"""`usage_ledger.record_usage` — the per-turn ledger row.

Same "no live Postgres" approach as tests/agent/test_usage_ledger.py: the module's
own `get_connection` is replaced by a fake that records the SQL and parameters it
was given, so these prove statement shape and the contract around it (what is
written, when nothing is, that a failure never reaches the turn) — not how a real
database behaves. The model lookup `record_usage` awaits is replaced per test; the
resolver itself is covered in tests/agent/test_model_resolver.py.
"""
from contextlib import asynccontextmanager

import pytest

from app.agent import usage_ledger
from tests.conftest import TEST_CTX


class _FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    async def execute(self, sql, params):
        self.calls.append((sql, list(params)))


def _fake_get_connection(fake):
    @asynccontextmanager
    async def get_connection():
        yield fake

    return get_connection


async def test_writes_the_resolved_model_alongside_the_tokens_and_cost(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

    async def resolve(alias):
        assert alias == "gpt-4o"
        return "openai/gpt-4o-2024-08-06"

    monkeypatch.setattr(usage_ledger, "resolve_model", resolve)

    await usage_ledger.record_usage(TEST_CTX, "thread-1", "gpt-4o", 2000)

    sql, params = fake.calls[0]
    assert "INSERT INTO usage_ledger" in sql
    assert params == [
        TEST_CTX["tenant"],
        TEST_CTX["principal"],
        "thread-1",
        "gpt-4o",
        2000,
        pytest.approx(0.01),  # 2 x $0.005 per 1k tokens
        "openai/gpt-4o-2024-08-06",
    ]


async def test_an_unresolvable_model_is_recorded_as_null_not_skipped(monkeypatch):
    fake = _FakeConnection()
    monkeypatch.setattr(usage_ledger, "get_connection", _fake_get_connection(fake))

    async def resolve(alias):
        return None

    monkeypatch.setattr(usage_ledger, "resolve_model", resolve)

    await usage_ledger.record_usage(TEST_CTX, "thread-1", "chat", 500)

    params = fake.calls[0][1]
    assert params[4] == 500 and params[-1] is None


@pytest.mark.parametrize(("ctx", "tokens"), [(None, 500), (TEST_CTX, 0), (TEST_CTX, -3)])
async def test_nothing_is_written_or_resolved_for_an_invalid_ctx_or_no_tokens(monkeypatch, ctx, tokens):
    async def _fail_if_called(*args):
        raise AssertionError("must not resolve a model or open a connection")

    monkeypatch.setattr(usage_ledger, "resolve_model", _fail_if_called)
    monkeypatch.setattr(usage_ledger, "get_connection", _fail_if_called)

    await usage_ledger.record_usage(ctx, "thread-1", "chat", tokens)  # must not raise


async def test_a_failing_write_never_fails_the_turn_it_records(monkeypatch):
    def _broken():
        raise ConnectionError("appdata postgres unreachable")

    monkeypatch.setattr(usage_ledger, "get_connection", _broken)

    await usage_ledger.record_usage(TEST_CTX, "thread-1", "chat", 500)  # must not raise
