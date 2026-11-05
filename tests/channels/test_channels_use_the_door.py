"""The CLI and the Telegram channel are clients of the chat API through the front door; neither reaches the work queue or the graph (specs/020).

They once published their turns straight to the queue from their own process (GRAPH_PATTERNS.md 89), with an identity they built themselves, so the
front door's limits, signed identity and revocation never applied to them and anything able to write to the queue could name any tenant and person.
This fails the build if a channel can do that again, by import: the channel modules may not import the graph, the runtime, the queue's publisher or
reader, or the module that used to wrap them.

Telegram keeps a little Redis state of its own (its poll offset, the inbox its updates wait in, its approval buttons and account links), through
`app.adapters.outbound.redis.queue.get_client` and nothing else from that module: it never publishes a turn.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHANNELS = sorted((REPO / "app" / "adapters" / "inbound" / "channels").glob("*.py"))
CHANNELS = [path for path in CHANNELS if path.name != "__init__.py"]

# Importing any of these runs the graph, builds it, or puts a turn on the queue.
FORBIDDEN = (
    "app.agent.graph",
    "app.agent.runtime.core",
    "app.agent.runtime.stream",
    "app.adapters.inbound.workers.agent_worker",
    "app.bootstrap",
)
QUEUE = "app.adapters.outbound.redis.queue"
ALLOWED_FROM_QUEUE = {"get_client"}


def imported(source: str) -> list[tuple[str, str | None]]:
    """(module, name) for every import; `name` is None for `import module`."""
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.extend((node.module, alias.name) for alias in node.names)
    return found


def _within(path: str, package: str) -> bool:
    return path == package or path.startswith(package + ".")


def offences(source: str) -> list[str]:
    bad: list[str] = []
    for module, name in imported(source):
        # `from a.b import c` may be importing the MODULE a.b.c, so both spellings are checked.
        paths = [module] + ([f"{module}.{name}"] if name else [])
        if any(_within(path, f) for path in paths for f in FORBIDDEN):
            bad.append(f"{module}{'.' + name if name else ''}")
        if module == QUEUE and name not in ALLOWED_FROM_QUEUE:
            bad.append(f"{module}{'.' + name if name else ''}")
        if module == "app.adapters.outbound.redis" and name == "queue":
            bad.append("app.adapters.outbound.redis.queue")
    return bad


def test_there_are_channels_for_this_test_to_be_about():
    """A floor against a vacuous pass: a moved directory would make every check below true."""
    names = {path.name for path in CHANNELS}
    assert {"chat.py", "telegram.py", "door.py", "oauth.py", "cli_auth.py", "telegram_people.py", "telegram_inbox.py", "telegram_link.py", "telegram_server.py"} <= names


@pytest.mark.parametrize("channel", CHANNELS, ids=lambda p: p.name)
def test_a_channel_cannot_reach_the_graph_or_the_queue(channel):
    assert offences(channel.read_text()) == [], (
        f"{channel.name} must send its turns to the chat API through the front door (app/adapters/inbound/channels/door.py), never to the queue or the graph"
    )


@pytest.mark.parametrize("channel", ["chat.py", "telegram.py"])
def test_the_two_channels_reach_the_agent_through_the_door(channel):
    modules = {module for module, _ in imported((REPO / "app" / "adapters" / "inbound" / "channels" / channel).read_text())}

    assert "app.adapters.inbound.channels.door" in modules


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from app.adapters.outbound.redis.queue import publish_request\n", True),
        ("from app.adapters.outbound.redis.queue import get_client, read_results\n", True),
        ("from app.adapters.outbound.redis import queue\n", True),
        ("import app.adapters.outbound.redis.queue\n", True),
        ("from app.agent.runtime import stream\n", True),
        ("from app.agent.graph.build import build_graph\n", True),
        ("from app.bootstrap import wire\n", True),
        # what a channel may do
        ("from app.adapters.outbound.redis.queue import get_client\n", False),
        ("from app.agent.runtime.sessions import TELEGRAM_THREAD_PREFIX\n", False),
        ("from app.adapters.inbound.channels.door import Door\n", False),
    ],
)
def test_the_check_flags_what_it_should(source, flagged):
    """The checker itself, so a regression in it cannot pass for a clean channel."""
    assert bool(offences(source)) is flagged
