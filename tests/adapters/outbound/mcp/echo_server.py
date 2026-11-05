"""A tiny stdio MCP server for the connection tests: just enough tools to see a process, kill one, and count side effects.

Deliberately imports nothing from the app, so it starts in a fraction of a second and the tests spawn it for real.
"""
import asyncio
import os

from mcp.server import MCPServer

mcp = MCPServer("echo")


@mcp.tool()
async def echo(text: str) -> str:
    return text


@mcp.tool()
async def pid() -> str:
    return str(os.getpid())


@mcp.tool()
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "slept"


@mcp.tool()
async def bump(path: str) -> str:
    """A side effect that must not run twice: appends one line to `path`."""
    with open(path, "a") as f:
        f.write("ran\n")
    return "bumped"


@mcp.tool()
async def bump_then_die(path: str) -> str:
    """Runs the side effect and then dies before it can answer: the call that may have run, whose reply was lost."""
    with open(path, "a") as f:
        f.write("ran\n")
    os._exit(3)


@mcp.tool()
async def die() -> str:
    os._exit(3)


if __name__ == "__main__":
    mcp.run(transport="stdio")
