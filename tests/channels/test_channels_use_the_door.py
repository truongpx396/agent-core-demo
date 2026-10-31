"""Every chat front door runs its turns on the queue; none runs the graph in its own process.

The CLI and the Telegram channel used to call `astream_events_turn*` and open the checkpointer
themselves, which gave them a second execution path that the worker's guarantees did not reach:
the per-thread lock (two messages into one conversation could overlap), crash reclaim and the
dead-letter archive. They now publish through `app/job_queue/producer.py`, and this fails the build
if one imports the graph runtime again, which would quietly bring the second path back.

What a channel may still import from `app.agent.runtime` is `sessions` (a constant: the reserved
`telegram:` thread prefix), not the graph or a streaming turn.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHANNELS = [REPO / "app" / "channels" / "chat.py", REPO / "app" / "channels" / "telegram.py"]

# Importing any of these runs, or builds, the graph.
THE_GRAPH = ("app.agent.graph", "app.agent.runtime.core", "app.agent.runtime.stream")


def _imported_names(path: Path) -> set[str]:
    """Every module an import statement in `path` names, and every `module.name` a `from` import
    could be reading as a submodule (`from app.agent.runtime import stream`)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_there_are_channels_for_this_test_to_be_about():
    """A floor against a vacuous pass: a path that no longer exists would make every check below
    true."""
    assert all(path.is_file() for path in CHANNELS) and len(CHANNELS) == 2


@pytest.mark.parametrize("channel", CHANNELS, ids=lambda p: p.name)
def test_a_channel_does_not_import_the_graph_runtime(channel):
    imported = _imported_names(channel)

    offending = sorted(
        name for name in imported if any(name == g or name.startswith(g + ".") for g in THE_GRAPH)
    )

    assert not offending, (
        f"{channel.name} imports {offending}: a channel publishes its turn to the queue "
        "(app/job_queue/producer.py) and a worker runs it"
    )


@pytest.mark.parametrize("channel", CHANNELS, ids=lambda p: p.name)
def test_a_channel_reaches_the_graph_through_the_producer(channel):
    assert "app.job_queue.producer" in _imported_names(channel)


@pytest.mark.parametrize("channel", CHANNELS, ids=lambda p: p.name)
def test_a_channel_does_not_call_a_streaming_turn_by_name(channel):
    """Catches the call through an alias or a module attribute that the import check cannot see
    (`runtime.astream_events_turn(...)`)."""
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(channel.read_text()))
        if isinstance(node, ast.Call)
    }

    assert not {name for name in called if name.startswith("astream_events_")}
