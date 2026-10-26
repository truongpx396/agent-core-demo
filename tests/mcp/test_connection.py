"""app/mcp/connection.py against a REAL stdio server (tests/mcp/echo_server.py): one process serves many calls, and what happens
when it dies is the behaviour measured while designing this, not assumed.

The server is a few lines of MCPServer that imports nothing from the app, so each test spawns it for real in a fraction of a second.
The unit tests at the bottom replace the session with a stand-in to reach the paths a real server makes awkward to trigger.
"""
import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest
from mcp import MCPError, types
from mcp.client.stdio import StdioServerParameters

from app.core import metrics
from app.mcp import connection as mcp_connection
from app.mcp.connection import McpConnection, close_all_connections, connection_for
from tests.conftest import metric_value

SERVER = str(Path(__file__).with_name("echo_server.py"))
PARAMS = StdioServerParameters(command=sys.executable, args=[SERVER])


@pytest.fixture(autouse=True)
async def _close_connections_after_each_test():
    yield
    await close_all_connections()


def _events(dependency: str) -> dict[str, float]:
    return {e: metric_value(metrics.agent_mcp_connection_events_total, dependency=dependency, event=e)
            for e in ("started", "restarted", "lost", "closed")}


async def _pid(conn: McpConnection) -> int:
    return int((await conn.call_tool("pid", {})).content[0].text)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(pid: int, within: float = 5.0) -> bool:
    for _ in range(int(within / 0.05)):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


class TestOneProcessServesManyCalls:
    async def test_every_call_reaches_the_same_server_process(self):
        conn = connection_for(PARAMS, "t_reuse")

        pids = {await _pid(conn) for _ in range(5)}

        assert len(pids) == 1
        assert _events("t_reuse")["started"] == 1

    async def test_the_same_server_on_the_same_loop_is_the_same_connection(self):
        assert connection_for(PARAMS, "t_same") is connection_for(StdioServerParameters(command=sys.executable, args=[SERVER]), "t_same")
        assert connection_for(PARAMS, "t_same") is not connection_for(StdioServerParameters(command=sys.executable, args=[SERVER, "x"]), "t_same")

    async def test_concurrent_calls_share_the_connection_and_each_gets_its_own_answer(self):
        conn = connection_for(PARAMS, "t_concurrent")

        results = await asyncio.gather(*(conn.call_tool("echo", {"text": str(i)}) for i in range(10)))

        assert [r.content[0].text for r in results] == [str(i) for i in range(10)]
        assert _events("t_concurrent")["started"] == 1

    async def test_two_tasks_that_open_it_at_once_start_one_process_not_two(self):
        conn = connection_for(PARAMS, "t_race")

        pids = await asyncio.gather(*(_pid(conn) for _ in range(6)))

        assert len(set(pids)) == 1
        assert _events("t_race")["started"] == 1

    async def test_a_call_that_times_out_locally_leaves_the_connection_usable(self):
        conn = connection_for(PARAMS, "t_timeout")
        before = await _pid(conn)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(conn.call_tool("slow", {"seconds": 5}), 0.2)

        assert await _pid(conn) == before


class TestAServerThatDies:
    async def test_the_connection_is_on_a_protocol_revision_where_the_liveness_ping_exists(self):
        """The no-replay guard pings before a call it must not repeat. Measured on mcp 2.3.0: the SDK's `Client` settles on
        protocol 2026-07-28, where `ping` is REMOVED and answers MCPError(-32601), so a connection opened that way would fail every
        sandbox command at the probe. Until another probe replaces it, this connection must stay on a revision that has the ping."""
        conn = connection_for(PARAMS, "t_ping")
        await _pid(conn)
        assert conn._link is not None and conn._link.session is not None

        await conn._link.session.send_ping()  # raises MCPError(-32601, "Method not found") on a revision without it

    async def test_found_dead_between_calls_the_next_call_runs_on_a_new_process_without_the_caller_noticing(self):
        conn = connection_for(PARAMS, "t_idle_death")
        first = await _pid(conn)
        os.kill(first, signal.SIGKILL)
        assert await _gone(first)

        second = await _pid(conn)

        assert second != first
        events = _events("t_idle_death")
        assert (events["started"], events["restarted"], events["lost"]) == (1, 1, 1)

    async def test_a_call_in_flight_when_the_server_dies_is_not_replayed(self, tmp_path):
        """Principle IV: the side effect ran before the reply was lost. Sending the call again would run it twice."""
        marker = tmp_path / "side-effects"
        conn = connection_for(PARAMS, "t_in_flight")

        with pytest.raises(ConnectionError):
            await conn.call_tool("bump_then_die", {"path": str(marker)})

        assert marker.read_text().splitlines() == ["ran"]  # once, not twice
        assert _events("t_in_flight")["lost"] == 1

    async def test_a_side_effect_called_after_an_idle_death_runs_exactly_once_on_the_new_process(self, tmp_path):
        """The server dies while nothing is in flight. mcp 2.x answers the next call on the dead session with the same
        CONNECTION_CLOSED as a call lost in flight, so only the ping before the call can say it never left; without it this call
        would either fail (never retried) or, if it were retried on that error alone, run twice."""
        marker = tmp_path / "side-effects"
        conn = connection_for(PARAMS, "t_idle_side_effect")
        first = await _pid(conn)
        os.kill(first, signal.SIGKILL)
        assert await _gone(first)

        result = await conn.call_tool("bump", {"path": str(marker)})

        assert result.content[0].text == "bumped"
        assert marker.read_text().splitlines() == ["ran"]

    async def test_after_losing_one_in_flight_the_next_call_reconnects_and_works(self, tmp_path):
        conn = connection_for(PARAMS, "t_recover")
        with pytest.raises(ConnectionError):
            await conn.call_tool("die", {})

        result = await conn.call_tool("echo", {"text": "back"})

        assert result.content[0].text == "back"
        assert _events("t_recover")["restarted"] == 1

    async def test_three_timeouts_in_a_row_recycle_a_connection_that_is_wedged_not_slow(self):
        conn = connection_for(PARAMS, "t_wedged")
        first = await _pid(conn)

        for _ in range(mcp_connection.WEDGED_AFTER_CANCELLED_CALLS):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(conn.call_tool("slow", {"seconds": 30}), 0.1)

        assert await _pid(conn) != first
        assert await _gone(first)  # the wedged process was terminated, not left behind

    async def test_a_success_between_timeouts_resets_the_count(self):
        conn = connection_for(PARAMS, "t_not_wedged")
        first = await _pid(conn)

        for _ in range(mcp_connection.WEDGED_AFTER_CANCELLED_CALLS * 2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(conn.call_tool("slow", {"seconds": 30}), 0.1)
            await conn.call_tool("echo", {"text": "still here"})

        assert await _pid(conn) == first


class TestStartingAndStopping:
    async def test_a_server_that_cannot_start_raises_the_real_error_not_a_task_group_wrapper(self):
        conn = connection_for(StdioServerParameters(command="/nonexistent/mcp-server-binary"), "t_nostart")

        with pytest.raises(FileNotFoundError):
            await conn.list_tools()

    async def test_a_failed_start_is_tried_again_by_the_next_call(self):
        conn = connection_for(StdioServerParameters(command="/nonexistent/mcp-server-binary"), "t_retry_start")
        for _ in range(2):
            with pytest.raises(FileNotFoundError):
                await conn.list_tools()

    async def test_closing_terminates_the_server_process(self):
        conn = connection_for(PARAMS, "t_close")
        pid = await _pid(conn)

        await close_all_connections()

        assert await _gone(pid)
        assert _events("t_close")["closed"] == 1

    async def test_a_loop_that_ends_without_closing_still_takes_the_child_with_it(self, tmp_path):
        """What a script that ends its `asyncio.run` without calling close_all_connections() relies on."""
        pidfile = tmp_path / "pid"
        script = (
            "import asyncio, sys\n"
            "from mcp.client.stdio import StdioServerParameters\n"
            "from app.mcp.connection import connection_for\n"
            "async def main():\n"
            f"    conn = connection_for(StdioServerParameters(command=sys.executable, args=[{SERVER!r}]), 'script')\n"
            f"    open({str(pidfile)!r}, 'w').write(str((await conn.call_tool('pid', {{}})).content[0].text))\n"
            "asyncio.run(main())\n"
        )
        repo = Path(__file__).resolve().parents[2]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(repo / p) for p in (".", "packages/billing/src", "docker/ml-service"))}
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, env=env, cwd=tmp_path)
        assert await asyncio.wait_for(proc.wait(), 90) == 0

        assert await _gone(int(pidfile.read_text()))


class _Session:
    """Stands in for a ClientSession; each call pops the next scripted outcome.

    `alive=False` is a session whose server is gone: like the real one on mcp 2.x, it answers a ping, or anything else, with
    CONNECTION_CLOSED before sending a thing. `sent` counts only the requests that would have reached a server."""

    def __init__(self, *outcomes, alive=True):
        self.outcomes = list(outcomes)
        self.alive = alive
        self.sent = 0
        self.pings = 0

    async def send_ping(self):
        self.pings += 1
        if not self.alive:
            raise _closed()

    async def call_tool(self, name, arguments):
        if not self.alive:
            raise _closed()
        self.sent += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def list_tools(self):
        if not self.alive:
            raise _closed()
        self.sent += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return types.ListToolsResult(tools=outcome)


def _scripted(monkeypatch, *sessions):
    """A connection whose successive starts hand out the given sessions."""
    conn = McpConnection(PARAMS, dependency="t_unit")
    queue = list(sessions)

    async def fake_start():
        link = mcp_connection._Link()
        link.session = queue.pop(0)
        link.task = asyncio.get_running_loop().create_future()  # "alive" until we resolve it
        return link

    monkeypatch.setattr(conn, "_start", fake_start)
    return conn


_OK = types.CallToolResult(content=[types.TextContent(type="text", text="ok")])


def _closed() -> MCPError:
    return MCPError(code=types.CONNECTION_CLOSED, message="Connection closed")


class TestReplayRules:
    async def test_a_listing_is_replayed_even_when_the_connection_dropped_in_flight(self, monkeypatch):
        dying, healthy = _Session(_closed()), _Session([types.Tool(name="t", input_schema={"type": "object"})])
        conn = _scripted(monkeypatch, dying, healthy)

        tools = await conn.list_tools()

        assert [t.name for t in tools] == ["t"]
        assert (dying.sent, healthy.sent) == (1, 1)
        assert (dying.pings, healthy.pings) == (0, 0)  # a read-only request needs no ping first

    async def test_a_tool_call_in_flight_is_never_replayed(self, monkeypatch):
        dying, healthy = _Session(_closed()), _Session(_OK)
        conn = _scripted(monkeypatch, dying, healthy)

        with pytest.raises(ConnectionError):
            await conn.call_tool("run", {})

        assert (dying.pings, dying.sent) == (1, 1)  # it answered the ping, took the call, and then the connection went
        assert (healthy.pings, healthy.sent) == (0, 0)

    async def test_a_call_that_never_left_is_sent_once_on_a_new_connection(self, monkeypatch):
        gone, healthy = _Session(alive=False), _Session(_OK)
        conn = _scripted(monkeypatch, gone, healthy)

        result = await conn.call_tool("run", {})

        assert result is _OK
        assert (gone.pings, gone.sent) == (1, 0)  # the ping found it closed; the call itself never went anywhere
        assert (healthy.pings, healthy.sent) == (1, 1)

    async def test_a_server_that_keeps_being_gone_gives_up_after_one_new_connection(self, monkeypatch):
        a, b = _Session(alive=False), _Session(alive=False)
        conn = _scripted(monkeypatch, a, b)

        with pytest.raises(ConnectionError):
            await conn.call_tool("run", {})

        assert (a.sent, b.sent) == (0, 0)

    async def test_a_protocol_error_from_a_live_server_is_not_a_lost_connection(self, monkeypatch):
        refusal = MCPError(code=types.INVALID_PARAMS, message="bad arguments")
        session = _Session(refusal)
        conn = _scripted(monkeypatch, session)

        with pytest.raises(MCPError):
            await conn.call_tool("run", {})
        before = _events("t_unit")["lost"]

        assert conn._link is not None  # still the connection in use
        assert _events("t_unit")["lost"] == before

    async def test_a_protocol_error_from_the_ping_is_raised_not_swallowed_as_a_dead_connection(self, monkeypatch):
        class _Refusing(_Session):
            async def send_ping(self):
                raise MCPError(code=types.INVALID_PARAMS, message="no ping here")

        session = _Refusing(_OK)
        conn = _scripted(monkeypatch, session)

        with pytest.raises(MCPError):
            await conn.call_tool("run", {})

        assert session.sent == 0
