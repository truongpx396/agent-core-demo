"""The API and the agent worker end their MCP connections on the way out (app/mcp/connection.py).

Each connection is a child process (the sandbox bridge). Left to the event loop's teardown it would still be cancelled, but closing
on the loop that served the requests is the orderly path, and it is the one a SIGTERM'd worker takes. Neither entry point's shutdown
runs anywhere else in the suite (the API's lifespan never runs under the in-process test client), so this runs both for real with
their collaborators replaced.
"""
import asyncio
import os
import signal
from unittest.mock import AsyncMock, MagicMock

from app.api import main as api_main
from app.job_queue import agent_worker


async def test_the_api_closes_its_mcp_connections_when_the_lifespan_ends(monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "configure_telemetry", lambda name: None)
    monkeypatch.setattr(api_main.billing_http, "validate_configuration", lambda: None)
    monkeypatch.setattr(api_main, "init_graph_async", AsyncMock())
    monkeypatch.setattr(api_main, "close_all_connections", AsyncMock(side_effect=lambda: calls.append("mcp")))
    monkeypatch.setattr(api_main.sql_store, "close_pool", AsyncMock(side_effect=lambda: calls.append("sql")))
    monkeypatch.setattr(api_main, "close_checkpointer_pool", AsyncMock(side_effect=lambda: calls.append("checkpointer")))

    async with api_main.lifespan(api_main.app):
        assert calls == []  # nothing closes while serving

    assert calls == ["mcp", "sql", "checkpointer"]


async def test_a_worker_told_to_stop_closes_its_mcp_connections_after_its_in_flight_jobs(monkeypatch):
    calls = []
    client = MagicMock()
    client.aclose = AsyncMock()

    async def read_then_ask_to_stop(*args, **kwargs):
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL  # run() installed its handler: this will not kill pytest
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
        return []

    client.xreadgroup = read_then_ask_to_stop

    async def no_reclaim(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_worker, "resolve_domain", lambda name: (MagicMock(), MagicMock()))
    monkeypatch.setattr(agent_worker, "init_graph_async", AsyncMock())
    monkeypatch.setattr(agent_worker, "get_client", lambda: client)
    monkeypatch.setattr(agent_worker, "ensure_consumer_group", AsyncMock())
    monkeypatch.setattr(agent_worker, "_reclaim_loop", no_reclaim)
    monkeypatch.setattr(agent_worker, "close_all_connections", AsyncMock(side_effect=lambda: calls.append("mcp")))
    monkeypatch.setattr(agent_worker.sql_store, "close_pool", AsyncMock(side_effect=lambda: calls.append("sql")))
    monkeypatch.setattr(agent_worker, "close_checkpointer_pool", AsyncMock(side_effect=lambda: calls.append("checkpointer")))

    await asyncio.wait_for(agent_worker.run(), 30)

    assert calls == ["mcp", "sql", "checkpointer"]
    client.aclose.assert_awaited_once()
