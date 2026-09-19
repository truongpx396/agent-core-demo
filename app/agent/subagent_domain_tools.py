"""`make_domain_subagent_tool` — builds a per-domain `run_subagent` tool
(the domain equivalent of `app/agent/subagent_tools.py`'s own Ecorp-level
construction). Split out of `subagent_tools.py` purely for file size — see
that module's own docstring for the rest of the `run_subagent` machinery
(`_run_subagent_impl`, `_build_subagent_registry`, the compiled-graph
cache) this function calls into. No behavior change from the pre-split
single-file version.
"""
from collections.abc import Mapping
from enum import Enum
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator

from app.agent import subagent_tools as subagent_tools_module
from app.agent.subagent_tools import _build_subagent_registry


def make_domain_subagent_tool(
    domain: str, all_tools: list, tool_capabilities: Mapping[str, str]
) -> BaseTool | None:
    """Builds a `run_subagent` tool scoped to `domain` — the domain
    equivalent of the Ecorp-level construction above (same closed-enum
    menu, same `RunSubagentArgs` shape, same mandatory read_only-only
    resolution via `_resolve_subagent_tools`), but resolved against
    `all_tools`/`tool_capabilities` — the CALLING domain's own tool
    universe, e.g. app/domains/support/domain.py passes its own ticket
    tools plus its own `search_docs`/`skill_search`/`use_skill`/
    `ask_clarification`, not Ecorp's `TOOLS`/`TOOL_CAPABILITIES` — and
    filtered to only the subagents actually declared for `domain`
    (`domains: [...]` frontmatter, see app/agent/subagents.py's docstring
    for the "untagged means Ecorp-only" default this applies).

    Returns `None` if that filtered registry ends up empty — a domain with
    no bundled subagent gets no `run_subagent` tool at all, never one
    offering an empty menu: a `SubagentName`-style enum needs at least one
    real member to be a meaningful closed vocabulary, and a tool a caller
    can never usefully invoke is worse than no tool (same "that omission is
    what sandboxed means" posture app/domains/support/domain.py's own
    docstring already takes for tools this app deliberately doesn't expose).

    Each call builds a genuinely NEW, distinct closure (its own Enum class,
    `Args` schema, and `run_subagent` tool object) — never the Ecorp-level
    `run_subagent` above. Meant to be called ONCE, at each domain module's
    own import time (same "built once, not lazily" reasoning the
    Ecorp-level block's own comment gives — a subagent added to disk after
    the process starts needs a restart to appear here either way).
    """
    all_tool_names = frozenset(t.name for t in all_tools)
    registry = _build_subagent_registry(all_tool_names, tool_capabilities, domain=domain)
    if not registry:
        return None

    tools_by_name = {t.name: t for t in all_tools}

    # A per-domain Enum TYPE (not just distinct member values) — reusing
    # SubagentName here would mix this domain's menu with Ecorp's, and two
    # domains both calling this factory would silently share one Enum
    # class between them, wrong the moment their registries diverge.
    domain_subagent_name = Enum(  # type: ignore[misc]
        f"SubagentName_{domain}", {name: name for name in sorted(registry)}, type=str
    )

    menu = "\n".join(
        f"- {name}: {record.description}" for name, (record, _tools) in sorted(registry.items())
    )

    class _RunSubagentArgs(BaseModel):
        subagent_name: domain_subagent_name = Field(  # type: ignore[valid-type]
            ..., description=f"Which subagent to delegate to. Options:\n{menu}"
        )
        task: str = Field(
            ...,
            description="The self-contained task to delegate. The subagent has NO "
            "access to this conversation's history, so include everything it needs "
            "to know in this one description.",
        )
        # See RunSubagentArgs's identical field above for why this must be
        # declared on the schema itself, not just the wrapper function below.
        tool_call_id: Annotated[str, InjectedToolCallId]

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=_RunSubagentArgs)
    async def run_subagent(
        subagent_name: domain_subagent_name,
        task: str,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Delegate a self-contained task to a specialized subagent running in
        its own isolated context — it does NOT see this conversation's
        history, only the `task` description you give it, so describe
        everything it needs to know. Use this to keep a multi-step lookup's
        intermediate steps out of the main conversation, or when a task
        matches a subagent's specific focus better than doing it yourself.
        Every subagent is restricted to read_only tools, so calling this
        never needs human approval."""
        # Read as subagent_tools_module._run_subagent_impl, not a plain
        # statically-imported bare name — tests/agent/test_tools.py does
        # monkeypatch.setattr(subagent_tools, "_run_subagent_impl", ...),
        # patching an attribute on the live `app.agent.subagent_tools`
        # module object. A bare name here would bind to the ORIGINAL
        # function once, at this module's own import time, permanently, so
        # the monkeypatch would silently never take effect — same real
        # bug/fix as `app/agent/graph_hitl.py`'s own `graph_module.interrupt`
        # (see its docstring).
        result = await subagent_tools_module._run_subagent_impl(
            subagent_name.value,
            task,
            config,
            domain=domain,
            registry=registry,
            tools_by_name=tools_by_name,
            use_cache=True,
        )
        return Command(
            update={
                "messages": [ToolMessage(content=result.answer, tool_call_id=tool_call_id)],
                "subagent_spend": [(result.total_tokens, result.total_cost_usd)],
            }
        )

    return run_subagent
