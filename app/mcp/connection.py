"""One long-lived stdio connection to a remote MCP server, shared by every call on an event loop.

`app/mcp/client.py` used to open a fresh connection for every call: spawn the server's process, run the protocol handshake, make
one call, tear it all down. For the sandbox that is a Python interpreter importing the OpenSandbox SDK, up to four times for one
`run_python_in_sandbox` (list, create, write, run). A connection kept open pays that once per process.

## Who owns the connection

`stdio_client` and `ClientSession` are async context managers built on task groups, which must be entered and exited by the SAME
task; a request handler that opened one could not hand it to the next request. So one dedicated task per connection (the "owner")
enters both, publishes the session, and sleeps until told to stop. Callers use the session from their own tasks, many at once: the
protocol multiplexes requests on the one pipe (measured: five concurrent calls, 12 ms). The owner starts in an EMPTY context, so it
does not keep the trace or request id of whichever request happened to open it.

## What a failure does (and does not) do

Measured against a real server on mcp 2.3.0, not assumed: a call that times out locally leaves the session usable; a server that
dies while a call is in flight fails that call with `MCPError(CONNECTION_CLOSED)`; and a call made AFTER the server died fails with
the very same error, before anything is sent. The SDK gives the two no different exception, and a lost call must never be
sent twice, so the connection has to find out another way whether a call could have reached the server. The consequences:

  * A connection found dead is dropped and the next call starts a new one (`agent_mcp_connection_events_total`).
  * Before a call that is not safe to repeat, the connection pings the server (about a millisecond on a local pipe). A ping that
    finds the connection closed proves the call was never sent, so it is sent once on a fresh connection; a dead server executes
    nothing. A server that dies between the ping and the call is the only gap, and it fails the call instead of repeating it.
  * A call that was IN FLIGHT when the server died is NOT replayed, whatever it was. It may have run to completion before the
    reply was lost, and a sandbox command or file write is not safe to run twice (constitution Principle IV; the same reason
    `app/core/resilience.py` retries only what "never landed"). The caller gets `ConnectionError`, and the next call reconnects.
    A read-only listing, which cannot do harm twice, is replayed without the ping.
  * Three calls in a row that time out locally, with no success between, mean the server is wedged rather than slow: the
    connection is recycled, so a hung bridge costs one respawn instead of every later call waiting out its own timeout.

The server on the other end may run a different MCP SDK from this process (the sandbox bridge is on mcp 1.x, see
`app/domains/sandbox_tools.py`). `initialize()` is the handshake both understand; the SDK's `Client` class would probe with
`server/discover` first, which a 1.x server answers with a validation error on its stderr at every connect.

## Lifetime

A connection belongs to the event loop that opened it (its streams are bound to it), so they are kept per loop, and
`close_all_connections()` closes the current loop's at shutdown (`app/api/main.py`'s lifespan, `agent_worker`'s exit). A loop that
ends without closing them (`asyncio.run` in a script) cancels the owner on its way out, which terminates the child; a server whose
parent vanishes exits on end-of-input anyway.
"""
import asyncio
import contextvars
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from mcp import ClientSession, MCPError, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.core import metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")

START_TIMEOUT_SECONDS = 30.0  # a server that has not answered the handshake by now is not going to
CLOSE_TIMEOUT_SECONDS = 10.0  # the SDK waits up to 2s for the child to exit before killing it
WEDGED_AFTER_CANCELLED_CALLS = 3


def _unwrap(exc: BaseException) -> BaseException:
    """The task groups inside the SDK wrap a failure in an ExceptionGroup of one; callers (the breaker's `retry_on`) match on the
    real exception type, so hand them that one."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


class _Link:
    """One running server process, its session, and the task that owns both."""

    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.task: asyncio.Task | None = None
        self.stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.error: BaseException | None = None
        self.cancelled_in_a_row = 0

    @property
    def alive(self) -> bool:
        return self.session is not None and self.task is not None and not self.task.done()


class McpConnection:
    """A reusable connection to one stdio MCP server. Create through `connection_for`, not directly."""

    def __init__(self, params: StdioServerParameters, *, dependency: str, start_timeout: float = START_TIMEOUT_SECONDS):
        self._params = params
        self._dependency = dependency
        self._start_timeout = start_timeout
        self._link: _Link | None = None
        self._lock = asyncio.Lock()
        self._started_before = False

    # -- the owner task ------------------------------------------------------------------------------------------------

    async def _own(self, link: _Link) -> None:
        try:
            async with stdio_client(self._params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                link.session = session
                link.ready.set()
                await link.stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - handed to the starter through link.error; after startup a dying server is routine
            link.error = _unwrap(exc)
            logger.debug("mcp_connection_ended", extra={"dependency": self._dependency, "error_class": type(link.error).__name__})
        finally:
            link.session = None
            link.ready.set()  # whoever is waiting for startup must wake, with link.error set if it failed

    async def _start(self) -> _Link:
        link = _Link()
        link.task = asyncio.get_running_loop().create_task(
            self._own(link), name=f"mcp-connection:{self._dependency}", context=contextvars.Context()
        )
        try:
            await asyncio.wait_for(link.ready.wait(), self._start_timeout)
        except BaseException:
            await self._abort(link)  # timed out, or the caller was cancelled: the owner may be stuck in the handshake
            raise
        if link.error is not None or not link.alive:
            raise link.error or ConnectionError(f"the MCP server for {self._dependency} ended during startup")
        return link

    @staticmethod
    async def _stop(link: _Link, timeout: float = CLOSE_TIMEOUT_SECONDS) -> None:
        """Ask the owner to unwind, which terminates the child, and wait for it; cancel it if it will not."""
        link.stop.set()
        task = link.task
        if task is None or task.done():
            return
        try:
            _, pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()  # the caller was cancelled while waiting: still leave no owner behind
            raise
        if pending:
            task.cancel()

    @staticmethod
    async def _abort(link: _Link) -> None:
        """Stop an owner that is not waiting on `stop` (it is still starting): cancel it and give it a moment to unwind."""
        link.stop.set()
        task = link.task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=CLOSE_TIMEOUT_SECONDS)

    # -- using it --------------------------------------------------------------------------------------------------------

    async def _live(self) -> _Link:
        link = self._link
        if link is not None and link.alive:
            return link
        async with self._lock:
            link = self._link
            if link is not None and link.alive:
                return link
            link = await self._start()
            self._link = link
            event = "restarted" if self._started_before else "started"
            self._started_before = True
            metrics.agent_mcp_connection_events_total.labels(dependency=self._dependency, event=event).inc()
            return link

    def _lost(self, link: _Link, exc: BaseException) -> None:
        """Drop a connection found dead (only if it is still THE connection: another task may have replaced it already)."""
        if self._link is link:
            self._link = None
        link.stop.set()
        metrics.agent_mcp_connection_events_total.labels(dependency=self._dependency, event="lost").inc()
        logger.warning(
            "mcp_connection_lost", extra={"dependency": self._dependency, "error_class": type(_unwrap(exc)).__name__}
        )

    async def _probe(self, link: _Link, session: ClientSession) -> MCPError | None:
        """Ping the server. `None` if it answers; the error, with the connection dropped, if it is gone. A ping is the one
        request that is harmless to send and tells whether a call that is NOT safe to repeat would reach anyone."""
        try:
            await session.send_ping()
        except MCPError as exc:
            if exc.code != types.CONNECTION_CLOSED:
                raise
            self._lost(link, exc)
            return exc
        return None

    async def _request(self, operation: Callable[[ClientSession], Awaitable[T]], *, replay_in_flight: bool) -> T:
        last: BaseException | None = None
        for _ in range(2):
            link = await self._live()
            session = link.session
            if session is None:  # the server ended between _live() and here: nothing was sent
                self._lost(link, ConnectionError("ended before the call"))
                continue
            try:
                if not replay_in_flight:
                    gone = await self._probe(link, session)
                    if gone is not None:  # found closed before the call left: safe to send on a new connection
                        last = gone
                        continue
                result = await operation(session)
            except MCPError as exc:
                if exc.code != types.CONNECTION_CLOSED:
                    raise  # a live server answering with a protocol error is not a lost connection
                self._lost(link, exc)
                last = exc
                if replay_in_flight:
                    continue
                break
            except asyncio.CancelledError:
                link.cancelled_in_a_row += 1
                if link.cancelled_in_a_row >= WEDGED_AFTER_CANCELLED_CALLS:
                    self._lost(link, TimeoutError("calls keep timing out"))
                raise
            link.cancelled_in_a_row = 0
            return result
        raise ConnectionError(f"the MCP connection to {self._dependency} was lost") from last

    async def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        """Call a tool. Never replayed once it may have reached the server (see the module docstring)."""
        return await self._request(lambda session: session.call_tool(name, arguments), replay_in_flight=False)

    async def list_tools(self) -> list[types.Tool]:
        """List the server's tools. Read-only, so it is replayed even when the connection drops mid-request."""

        async def operation(session: ClientSession) -> list[types.Tool]:
            return (await session.list_tools()).tools

        return await self._request(operation, replay_in_flight=True)

    async def aclose(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            await self._stop(link)
            metrics.agent_mcp_connection_events_total.labels(dependency=self._dependency, event="closed").inc()


# -- one connection per (event loop, server) ----------------------------------------------------------------------------

_connections: dict[asyncio.AbstractEventLoop, dict[str, McpConnection]] = {}


def connection_for(params: StdioServerParameters, dependency: str) -> McpConnection:
    """The connection for this server on the running loop, created on first use. `dependency` names it on metrics and logs and
    is taken from whichever call creates it."""
    loop = asyncio.get_running_loop()
    for stale in [known for known in _connections if known.is_closed()]:
        del _connections[stale]  # a loop that ended took its connections' tasks with it
    key = hashlib.sha256(params.model_dump_json().encode()).hexdigest()  # not the params themselves: they may hold a key
    per_loop = _connections.setdefault(loop, {})
    if key not in per_loop:
        per_loop[key] = McpConnection(params, dependency=dependency)
    return per_loop[key]


async def close_all_connections() -> None:
    """Close every connection opened on the running loop. Call at shutdown, on the loop that served the requests."""
    connections = _connections.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(*(conn.aclose() for conn in connections.values()), return_exceptions=True)
