"""Every process that runs the application binds the ports first (`app/bootstrap.py::wire`), or its first call to a port raises `PortNotBound`.

An entrypoint is a module that is run (`if __name__ == "__main__"`) or that builds the ASGI app. One that imports the application (the agent, a
domain, a port, an adapter, the queue, a channel, MCP) must call `wire()` itself, or run inside `scheduled_job`, which does. The scan is over the AST, so
a call in a nested function counts and a mention in a comment does not. This is the check that stops the next script from being the one that forgets.

`wire` is deliberately idempotent and never replaces an adapter someone bound (`Slot.provide`), so calling it from a place that is also reached from
a test or a second helper is harmless; the cost of a redundant call is a few imports, the cost of a missing one is a stack trace at the first request.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Importing one of these means the module reaches a port, directly or through what it calls.
APPLICATION_PACKAGES = (
    "app.agent", "app.domains", "app.ports", "app.adapters", "app.retrieval", "app.ingestion", "app.job_queue", "app.channels", "app.api",
    "app.mcp",
)

# (entrypoint, why it needs no wiring)
EXEMPT = {
    "scripts/litellm_key.py": "mints a gateway key over HTTP; it imports one pure helper (`admin_base_url`) from an adapter module and calls no port",
}


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _calls(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (isinstance(func, ast.Attribute) and func.attr == name):
                return True
    return False


def is_entrypoint(source: str) -> bool:
    return '__name__ == "__main__"' in source or "FastAPI(" in source


def needs_wiring(source: str) -> bool:
    return any(name == pkg or name.startswith(pkg + ".") for name in _imports(ast.parse(source)) for pkg in APPLICATION_PACKAGES)


def wires(source: str) -> bool:
    tree = ast.parse(source)
    return _calls(tree, "wire") or _calls(tree, "scheduled_job")


def unwired(files: dict[str, str]) -> list[str]:
    return sorted(
        rel for rel, source in files.items()
        if rel not in EXEMPT and is_entrypoint(source) and needs_wiring(source) and not wires(source)
    )


def _repo_files() -> dict[str, str]:
    paths = [*(REPO / "app").rglob("*.py"), *(REPO / "scripts").rglob("*.py")]
    return {p.relative_to(REPO).as_posix(): p.read_text() for p in paths}


def test_every_entrypoint_that_reaches_the_application_wires_the_ports():
    assert unwired(_repo_files()) == []


def test_there_are_entrypoints_for_this_rule_to_be_about():
    """A floor against a vacuous pass: a moved directory would leave nothing to check."""
    found = [rel for rel, source in _repo_files().items() if is_entrypoint(source) and needs_wiring(source)]
    assert len([rel for rel in found if rel.startswith("app/")]) >= 8, "the API, both workers, both channels and the three MCP servers"
    assert len([rel for rel in found if rel.startswith("scripts/")]) >= 10


def test_scheduled_job_wires_for_the_one_shot_jobs_that_run_inside_it():
    """`unwired` trusts `scheduled_job` to wire, so that trust is checked here."""
    assert _calls(ast.parse((REPO / "app/core/job_runtime.py").read_text()), "wire")


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ('from app.agent import cost\nif __name__ == "__main__":\n    cost.run()\n', True),
        ('from app.agent import cost\nfrom app.bootstrap import wire\nif __name__ == "__main__":\n    wire()\n    cost.run()\n', False),
        ('from app.agent import cost\nfrom app import bootstrap\ndef main():\n    bootstrap.wire()\nif __name__ == "__main__":\n    main()\n', False),
        ('from app.core.job_runtime import scheduled_job\nfrom app.domains import notify\nif __name__ == "__main__":\n    with scheduled_job("x"):\n        pass\n', False),
        ('from app.mcp import server\nif __name__ == "__main__":\n    server.main()\n', True),
        # not an entrypoint, or reaches nothing that needs a port
        ('from app.agent import cost\ndef helper():\n    cost.run()\n', False),
        ('import json\nfrom app.core.config import settings\nif __name__ == "__main__":\n    print(json.dumps({}))\n', False),
        # a mention in a comment or a string is not a call
        ('from app.agent import cost\n# wire()\nif __name__ == "__main__":\n    print("wire()")\n', True),
    ],
)
def test_the_scanner_flags_exactly_the_entrypoints_that_forgot(source, flagged):
    assert (unwired({"scripts/example.py": source}) == ["scripts/example.py"]) is flagged
