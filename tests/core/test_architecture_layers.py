"""The hexagonal dependency rule, enforced by the build (ARCHITECTURE.md; specs/019-hexagonal-architecture).

The application (the agent and its domains) depends on PORTS, `Protocol`s in `app/ports/`; the code that talks to Postgres,
Redis, Qdrant, the LLM gateway, the ML service or the web is an ADAPTER that implements one; and `app/bootstrap.py` is the only place that
binds the two. This file fails the build when an import crosses a ring the wrong way. Every rule is a statement about imports (an AST
scan, nested imports included, which is how a function-local `import psycopg` would otherwise slip past), and each has a test of the
scanner itself, because a guard that cannot fail proves nothing.

The rings, inside out:

- ports       `app/ports/**`. Protocols and plain data. Imports the standard library, `pydantic`, the kernel and other ports.
- kernel      `app/core/*.py` except the platform modules below. Security context, errors, scrubbing, the tool wrapper, metrics, settings.
              Imports no infrastructure client and nothing outside the kernel and the ports.
- application `app/agent/**` and `app/domains/**` except the adapter files below (a domain's `store.py`, and the registry that names every domain). The graph, the runtime, the cost policy, the tools.
              Imports no infrastructure client, no adapter and no inbound adapter; `app/agent` never imports `app/domains` (a domain plugs
              into the agent, not the other way round).
- adapters    everything else: `app/retrieval`, `app/ingestion`, `app/mcp`, `app/job_queue`, `app/api`, `app/channels`, `app/adapters`,
              the platform modules in `app/core`, and the per-domain `store.py` (the domain's repository, by the `store.py` + `tools.py` +
              `domain.py` convention). They may import anything inside them.

Accepted in the application ring, on purpose: LangGraph and LangChain core (the agent IS a LangGraph graph; `ARCHITECTURE.md` says why that
is a framework choice and not a leak), and the observability libraries (OpenTelemetry, Langfuse, structlog), which are cross-cutting.

`LEDGER` is the ratchet: today's violations that a later change in the same series removes. The test fails on a violation that is NOT
listed (so nothing new gets in) and on a listed one that no longer exists (so the list can only shrink). It is empty when the migration
is finished; an entry left in it is a disclosed gap, not an exception.
"""
from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Infrastructure clients: importing one means the module talks to a concrete system.
INFRASTRUCTURE = frozenset({
    "psycopg", "psycopg_pool", "redis", "qdrant_client", "openai", "langchain_openai", "httpx", "minio", "fastembed",
    "crawl4ai", "mcp", "fastapi", "starlette", "uvicorn", "jwt", "limits", "langgraph.checkpoint.postgres",
})

# Modules that live in `app/core` but are adapters or process plumbing, not kernel.
PLATFORM_CORE = frozenset({
    "sql_store", "internal_identity", "telemetry", "logging_config", "job_runtime",
})

# Application-ring files that are adapters by convention.
ADAPTER_FILE_GLOBS = ("app/domains/*/store.py", "app/domains/ops/metrics_client.py", "app/domains/registry.py")

OUTER_PACKAGES = (
    "app.retrieval", "app.ingestion", "app.mcp", "app.job_queue", "app.api", "app.channels", "app.adapters", "app.bootstrap",
)

# The one adapter-ring module the application may import: billing's port facade (specs/011), whose boundary has two tests of its own.
ALLOWED_OUTER = frozenset({"app.billing_integration.port"})

# Violations that remain, as (file, imported module). See the module docstring.
LEDGER: frozenset[tuple[str, str]] = frozenset({
})


def ring_of(rel: str) -> str:
    """The ring a repo-relative path belongs to."""
    if rel.startswith("app/ports/"):
        return "ports"
    if any(fnmatch.fnmatch(rel, glob) for glob in ADAPTER_FILE_GLOBS):
        return "adapter"
    if rel.startswith("app/core/"):
        stem = Path(rel).stem
        return "adapter" if stem in PLATFORM_CORE else "kernel"
    if rel.startswith(("app/agent/", "app/domains/")):
        return "application"
    return "adapter"


def _imports(tree: ast.AST) -> set[str]:
    """Every module an import statement names, and every `module.name` a `from` import could be reading as a submodule."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _is(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _match(name: str, prefixes) -> str | None:
    """The first of `prefixes` that `name` is, or lies under."""
    return next((prefix for prefix in prefixes if _is(name, prefix)), None)


PLATFORM_MODULES = tuple(f"app.core.{stem}" for stem in sorted(PLATFORM_CORE))


def violations_in(rel: str, source: str) -> set[tuple[str, str]]:
    """The (file, rule target) pairs in `source` that break the dependency rule for the ring `rel` belongs to. The target is the
    infrastructure package, adapter package or module the import reaches (`psycopg`, `app.core.sql_store`), not the spelling of the
    import, so `from psycopg import errors` and `import psycopg` are one entry."""
    ring = ring_of(rel)
    if ring == "adapter":
        return set()
    bad: set[tuple[str, str]] = set()
    for name in _imports(ast.parse(source)):
        target = _match(name, INFRASTRUCTURE)
        if target is None and name.startswith("app."):
            target = _match(name, OUTER_PACKAGES) or _match(name, PLATFORM_MODULES)
            if target is None and name != "app.billing_integration" and _is(name, "app.billing_integration") and not _match(name, ALLOWED_OUTER):
                target = "app.billing_integration"
            if target is None and ring in ("ports", "kernel"):
                target = _match(name, ("app.agent", "app.domains"))
            if target is None and rel.startswith("app/agent/"):
                target = _match(name, ("app.domains",))
            if target is None and ring == "application" and any(
                fnmatch.fnmatch(name.replace(".", "/") + ".py", glob) for glob in ADAPTER_FILE_GLOBS
            ):
                target = name
        if target is not None:
            bad.add((rel, target))
    return bad


def scan_repo() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((REPO / "app").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        found |= violations_in(rel, path.read_text())
    return found


def test_the_dependency_rule_holds_except_for_the_ledger():
    now = scan_repo()
    new = sorted(now - LEDGER)
    assert not new, (
        "an import crosses a ring the wrong way (ARCHITECTURE.md): depend on a port in app/ports/ and let app/bootstrap.py bind the adapter\n  "
        + "\n  ".join(f"{file} imports {module}" for file, module in new)
    )


def test_the_ledger_only_shrinks():
    stale = sorted(LEDGER - scan_repo())
    assert not stale, (
        "these ledger entries no longer exist: delete them so the ratchet tightens\n  "
        + "\n  ".join(f"{file} imports {module}" for file, module in stale)
    )


# --- two more shapes of the same rule -----------------------------------------------------------------------------------------

# The driving adapters are leaves: the API and the channels call into the application and nothing calls into them.
LEAF_PACKAGES = ("app.api", "app.channels")

# The composition root is imported by the code that STARTS a process (an entrypoint, or `scheduled_job` which starts the one-shot jobs), and by nothing else:
# anything deeper that imported it could bind adapters behind a test's back or make the application depend on its own wiring.
BOOTSTRAP = "app.bootstrap"
BOOTSTRAP_IMPORTERS_BESIDES_ENTRYPOINTS = frozenset({"app/core/job_runtime.py"})


def importers_of(package: str, files: dict[str, str]) -> set[str]:
    """Repo-relative paths in `files` that import `package`, excluding the package's own modules."""
    own = package.replace(".", "/")
    return {
        rel for rel, source in files.items()
        if not rel.startswith(own) and any(_is(name, package) for name in _imports(ast.parse(source)))
    }


def _app_files() -> dict[str, str]:
    return {p.relative_to(REPO).as_posix(): p.read_text() for p in sorted((REPO / "app").rglob("*.py"))}


def _is_entrypoint(source: str) -> bool:
    return '__name__ == "__main__"' in source or "FastAPI(" in source


@pytest.mark.parametrize("package", LEAF_PACKAGES)
def test_a_driving_adapter_is_a_leaf_nothing_else_in_the_app_imports_it(package):
    assert importers_of(package, _app_files()) == set()


def test_only_process_entrypoints_import_the_composition_root():
    files = _app_files()
    importers = importers_of(BOOTSTRAP, files)
    not_entrypoints = sorted(
        rel for rel in importers if not _is_entrypoint(files[rel]) and rel not in BOOTSTRAP_IMPORTERS_BESIDES_ENTRYPOINTS
    )
    assert not_entrypoints == [], "wire the ports where the process starts, not from inside the application"
    assert len(importers) >= 8, "a floor against a vacuous pass: the API, the workers, the channels and the MCP servers all start here"


def test_the_leaf_and_composition_scanners_can_fail():
    files = {
        "app/agent/x.py": "from app.api import deps\n",
        "app/api/deps.py": "import app.api.schemas\n",
        "app/agent/y.py": "import app.bootstrap\n",
        "app/ok.py": "from app.core import config\n",
    }
    assert importers_of("app.api", files) == {"app/agent/x.py"}, "a package's own imports of itself do not count"
    assert importers_of("app.bootstrap", files) == {"app/agent/y.py"}


# --- the rules that decide stay free of the agent framework -------------------------------------------------------------------

# Accepted in the application ring: LangGraph and LangChain core, because the agent IS a graph over LangChain messages. But the modules that hold the
# DECISIONS the constitution's principles rest on (who may see what, what a URL may be, what is scrubbed, what a budget allows, what a turn may spend,
# what a domain's policy permits) do not read a message object or a graph state, so they are testable, and can be read, without the framework. This list
# is what is true today; a module joins it when it is cleaned of the framework, and a framework import into one of these is a failure.
# NOT on it, on purpose: the graph's own nodes and guards (`routing.should_continue`, `hitl`, `tool_guards`, `loop_guards`, `skill_guards`, `output_checks`,
# `messages`, `state`). LangChain's messages are the graph's transport type (the checkpointed `State.messages`): a node reads them and the logic it calls
# runs on plain data, so mirroring them would add a mapper for an `isinstance` at each edge. ARCHITECTURE.md ("Decisions") has the reasoning.
FRAMEWORK = ("langgraph", "langchain", "langchain_core", "langchain_openai")
FRAMEWORK_FREE = (
    "app/core/security.py", "app/core/errors.py", "app/core/scrubbing.py", "app/core/untrusted.py", "app/core/routing_policy.py",
    "app/core/url_safety.py", "app/core/output_limits.py", "app/core/resilience.py", "app/core/moderation.py",
    "app/agent/cost/budgets.py", "app/agent/cost/pricing.py", "app/agent/cost/metering.py", "app/agent/cost/usage_events.py",
    "app/agent/cost/budget_holds.py", "app/agent/graph/citations.py", "app/agent/graph/links.py", "app/domains/policy.py",
    "app/agent/runtime/sessions.py",
)


def framework_imports(source: str) -> set[str]:
    return {name for name in _imports(ast.parse(source)) if any(_is(name, fw) for fw in FRAMEWORK)}


@pytest.mark.parametrize("rel", FRAMEWORK_FREE)
def test_the_modules_that_decide_import_no_agent_framework(rel):
    assert framework_imports((REPO / rel).read_text()) == set()


def test_the_framework_scanner_can_fail():
    assert framework_imports("from langchain_core.messages import AIMessage") == {"langchain_core.messages", "langchain_core.messages.AIMessage"}
    assert framework_imports("import langgraph.graph") == {"langgraph.graph"}
    assert framework_imports("from app.core import metrics\nimport json") == set()


# --- the scanner itself: each rule must be able to fail ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("rel", "source", "expected"),
    [
        # application: infrastructure clients and adapters are out
        ("app/agent/cost/x.py", "import psycopg", {"psycopg"}),
        ("app/agent/cost/x.py", "from psycopg import errors", {"psycopg"}),
        ("app/agent/cost/x.py", "def f():\n    import httpx\n", {"httpx"}),
        ("app/agent/cost/x.py", "from app.core.sql_store import get_connection", {"app.core.sql_store"}),
        ("app/agent/graph/x.py", "from app.retrieval import semantic_cache", {"app.retrieval"}),
        ("app/domains/support/tools.py", "from app.domains.support import store", {"app.domains.support.store"}),
        ("app/domains/support/tools.py", "from app.ingestion.web_crawler import render_url_to_markdown", {"app.ingestion"}),
        ("app/agent/runtime/x.py", "from app.domains.registry import resolve_domain", {"app.domains"}),
        # application: what is allowed
        ("app/agent/cost/x.py", "from app.ports.cost import UsageEventStore", set()),
        ("app/agent/cost/x.py", "from app.billing_integration import port as billing_port", set()),
        ("app/agent/graph/x.py", "from langgraph.graph import StateGraph\nfrom langchain_core.messages import AIMessage", set()),
        ("app/agent/graph/x.py", "from app.core import metrics\nfrom app.core.security import SecurityCtx", set()),
        ("app/domains/support/tools.py", "from app.ports.domains import AgentManifest", set()),
        # kernel and ports: inward only
        ("app/core/security.py", "from qdrant_client.models import Filter", {"qdrant_client"}),
        ("app/core/tool_idempotency.py", "from app.core.sql_store import get_connection", {"app.core.sql_store"}),
        ("app/core/errors.py", "from app.agent.graph import state", {"app.agent"}),
        ("app/ports/cost.py", "from app.core.security import SecurityCtx", set()),
        ("app/ports/cost.py", "import psycopg", {"psycopg"}),
        ("app/ports/cost.py", "from app.retrieval import qdrant_store", {"app.retrieval"}),
        # adapters may import anything
        ("app/adapters/postgres/x.py", "import psycopg\nfrom app.agent.cost import budgets", set()),
        ("app/domains/support/store.py", "from app.core.sql_store import get_connection", set()),
        ("app/api/routers/chat.py", "from app.agent.runtime import stream", set()),
        ("app/core/sql_store.py", "import psycopg_pool", set()),
    ],
)
def test_the_scanner_flags_exactly_the_wrong_way_imports(rel, source, expected):
    assert {module for _, module in violations_in(rel, source)} == expected


def test_a_from_import_of_a_submodule_is_seen():
    """`from app.core import sql_store` names the module only through the imported name."""
    assert violations_in("app/agent/cost/x.py", "from app.core import sql_store") == {("app/agent/cost/x.py", "app.core.sql_store")}


def test_the_rings_cover_every_python_file_under_app():
    rings = {ring_of(path.relative_to(REPO).as_posix()) for path in (REPO / "app").rglob("*.py")}
    assert rings <= {"ports", "kernel", "application", "adapter"} and {"kernel", "application", "adapter"} <= rings


def test_there_are_files_for_these_rules_to_be_about():
    """A floor against a vacuous pass: a moved directory would leave every ring empty and every rule true."""
    by_ring: dict[str, int] = {}
    for path in (REPO / "app").rglob("*.py"):
        ring = ring_of(path.relative_to(REPO).as_posix())
        by_ring[ring] = by_ring.get(ring, 0) + 1
    assert by_ring.get("application", 0) > 40 and by_ring.get("kernel", 0) > 10
