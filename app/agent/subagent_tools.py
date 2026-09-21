"""Everything behind `run_subagent` (GRAPH_PATTERNS.md pattern 46): bundled-
catalog resolution (`_resolve_subagent_tools`/`_build_subagent_registry`/
`_subagent_declared_for_domain`), the compiled-graph cache, `_run_subagent_impl`,
and the Ecorp-level `run_subagent` singleton. Split out of `app/agent/tools.py`
purely for file size — see that module's docstring, and
`app/agent/graph_hitl.py`/`app/agent/graph_utils.py` for analogous splits on
the graph.py side. No behavior change from the pre-split file.

The per-domain equivalent, `make_domain_subagent_tool`, lives in
`app/agent/subagent_domain_tools.py` — split out from THIS file (not
`tools.py`), since it only needs `_build_subagent_registry`/
`_run_subagent_impl` from here, one-directionally.

`_run_subagent_impl` reads `ChatOpenAI` via `tools_module.ChatOpenAI`, not a
statically-imported bare name — same fix as `app/agent/graph_hitl.py`'s
`graph_module.interrupt`: `tests/agent/test_concurrent_turns.py` does
`monkeypatch.setattr(tools_module, "ChatOpenAI", ...)`, patching the live
`app.agent.tools` module attribute. A bare import would bind the original
class once at import time and silently defeat the monkeypatch.

Second issue this split had to handle: the Ecorp-level `run_subagent` tool
used to be appended to `app.agent.tools.TOOLS` just by living physically
inside tools.py. Now that construction lives here — `tools.py`'s trailing
`from app.agent import subagent_tools` import (at the very end of that
file) preserves the guarantee that importing `tools.py` still triggers this
module to load and append `run_subagent` onto the same shared `TOOLS` list,
regardless of import order.
"""
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.agent import subagents as subagents_module
from app.agent import tools as tools_module
from app.agent.tools import (
    _NO_CTX_REFUSAL,
    TOOL_CAPABILITIES,
    TOOLS,
    _arun_with_timeout,
    _ctx_from_config,
    logger,
)
from app.core import metrics
from app.core.config import (
    CHAT_MODEL,
    MAX_SUBAGENT_COST_USD_PER_RUN,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    SUBAGENT_TIMEOUT_SECONDS,
)
from app.core.scrubbing import scrub
from app.core.security import DEFAULT_POLICY, valid_ctx

# --- Subagents (app/agent/subagents.py, GRAPH_PATTERNS.md pattern 46) -------
# run_subagent delegates to a fresh, ISOLATED nested agent run (a genuinely
# separate graph.invoke() against build_subagent_graph(), the same node
# functions as the top-level agent but a leaner one-shot topology) — not
# more instructions loaded into THIS agent's context (that's skill_search/
# use_skill). All tool-safety validation lives HERE (owns TOOL_CAPABILITIES);
# app/agent/subagents.py stays a pure, domain-agnostic disk parser.
#
# SUBAGENT_TIMEOUT_SECONDS (imported above, app/core/config.py) — same
# soft-timeout mechanism as TOOL_TIMEOUT_SECONDS, just longer since a
# multi-step nested loop needs more time than a single tool call.
# Settings-backed (not a bare module constant here anymore) for the same
# reason REQUEST_TIMEOUT_SECONDS is: a slow backend has a legitimate
# reason to widen a pure operational timeout — see that setting's own
# comment in app/core/config.py for the real CI failure that motivated
# moving this one too.

# Compiled nested graph + bound LLM client, reused across calls — topology/
# LLM/tools/manifest are static per (domain, subagent_name), so rebuilding
# per-call (a whole StateGraph compile + fresh ChatOpenAI().bind_tools())
# was pure waste. Same lazy, unlocked cache shape as
# app/agent/subagents.py's `_subagents_cache` (a benign first-use race is
# accepted here too). Populated/read only when a caller opts in via
# `_run_subagent_impl(..., use_cache=True)` — see that docstring for why.
_subagent_graph_cache: dict[tuple[str, str], Any] = {}


def reset_subagent_graph_cache() -> None:
    """Test/ops hook — mirrors app/agent/subagents.py::reload_subagents()."""
    global _subagent_graph_cache
    _subagent_graph_cache = {}


def _resolve_subagent_tools(
    subagent_name: str,
    declared_tools: tuple[str, ...] | None,
    all_tool_names: frozenset[str],
    tool_capabilities: Mapping[str, str],
) -> tuple[str, ...]:
    """A subagent's effective, SAFE tool subset — pure function, unit-tested
    directly (tests/agent/test_tools.py).

    `declared_tools=None` means "every read_only tool the domain exposes"
    (mirrors AgentManifest.allowed_tools's "empty means everything",
    narrowed to read_only-only — see GRAPH_PATTERNS.md pattern 46 for why
    v1 subagents are read_only-only at all). Any declared name that doesn't
    exist, or isn't read_only, is DROPPED with a warning, never upgraded.
    An empty resolved set is still valid — the subagent just answers from
    general knowledge.

    `run_subagent` itself is ALWAYS stripped, unconditionally — the actual
    recursion block. It's declared read_only itself, so read_only-ness alone
    wouldn't exclude it; this needs an explicit separate check.
    """
    if declared_tools is not None:
        candidates = declared_tools
    else:
        candidates = tuple(
            name for name in all_tool_names if tool_capabilities.get(name, "outward") == "read_only"
        )
    resolved: list[str] = []
    for name in candidates:
        if name == "run_subagent":
            continue
        if name not in all_tool_names:
            logger.warning(
                "subagent declares an unknown tool; dropping it",
                extra={"subagent_name": subagent_name, "tool_name": name},
            )
            continue
        if tool_capabilities.get(name, "outward") != "read_only":
            logger.warning(
                "subagent declares a non-read_only tool; dropping it",
                extra={"subagent_name": subagent_name, "tool_name": name},
            )
            continue
        resolved.append(name)
    return tuple(resolved)


_ALL_TOOL_NAMES = frozenset(t.name for t in TOOLS)  # computed before
# run_subagent is appended below — doesn't affect correctness (it's always
# stripped anyway), just clarity.


def _subagent_declared_for_domain(record: "subagents_module.SubagentRecord", domain: str) -> bool:
    """An AGENT.md with no `domains:` frontmatter (`record.domains is None`)
    stays visible only to `domain="ecorp"` — it doesn't silently become
    available to every new domain. See app/agent/subagents.py's docstring for
    why this default differs from SkillRecord's (there `None` means "every
    domain"): a subagent's declared `tools:` are only meaningful against ONE
    tool universe, so an untagged subagent handed to another domain would
    typically resolve to nothing useful anyway."""
    if record.domains is None:
        return domain == "ecorp"
    return domain in record.domains


def _build_subagent_registry(
    all_tool_names: frozenset[str], tool_capabilities: Mapping[str, str], *, domain: str
) -> dict[str, tuple["subagents_module.SubagentRecord", tuple[str, ...]]]:
    """Every bundled AGENT.md declared for `domain` (see
    `_subagent_declared_for_domain`), resolved to its safe, read_only tool
    subset within `all_tool_names`/`tool_capabilities` — domain-specific,
    since a nested run can only call tools that exist AND are read_only
    within the calling domain's own tool universe."""
    registry = {}
    for record in subagents_module.get_subagents().values():
        if not _subagent_declared_for_domain(record, domain):
            continue
        resolved = _resolve_subagent_tools(record.name, record.tools, all_tool_names, tool_capabilities)
        registry[record.name] = (record, resolved)
    return registry


_SUBAGENT_REGISTRY = _build_subagent_registry(_ALL_TOOL_NAMES, TOOL_CAPABILITIES, domain="ecorp")

_CITATION_MARKER_WARNING = (
    "Do not use '[n]'-style bracket citation markers in your final answer — "
    "that citation convention is reserved for the orchestrating agent's own "
    "retrieved context, not yours."
)
# ^ Necessary, not decorative: the PARENT's check_output/_ungrounded_claims_count
# (GRAPH_PATTERNS.md pattern 39) cross-checks [n] markers against
# state["citations"], which a subagent's retrieval never populates on the
# parent — without this, a grounded subagent answer could get flagged as
# ungrounded once folded into the reply.


@dataclass
class _SubagentDomainPlugin:
    """A throwaway DomainPlugin scoping a nested subagent run to EXACTLY its
    pre-resolved, pre-validated (read_only-only) tool subset.

    Used instead of `AgentManifest.allowed_tools` because that field treats
    an empty tuple as "no filter — expose everything" (see
    app/agent/manifest.py). A subagent legitimately left with zero usable
    tools must actually run with zero tools, not fall back to the full
    Ecorp set (including add_note/remember) — exactly the privilege-
    escalation-by-omission pattern 17's fail-closed discipline rules out.
    Passing the already-narrowed list as `tools()` sidesteps the ambiguity
    entirely.

    `tool_capabilities()` reports every `_tools` entry as `read_only`
    directly, not a lookup into the caller's own `TOOL_CAPABILITIES` (which
    for a domain-scoped subagent may not even contain that tool's name).
    `_tool_capability()` (app/agent/graph.py) defaults an undeclared name to
    `"outward"` — correct for the top-level graph where a human can approve
    a pause, wrong here, where a nested one-shot run has no resume path and
    would just look like a silent failure. Correct by construction: every
    name in `_tools` already passed `_resolve_subagent_tools`'s read_only
    filter.
    """

    _tools: list

    def tools(self) -> list:
        return list(self._tools)

    def tool_capabilities(self) -> dict[str, str]:
        return {t.name: "read_only" for t in self._tools}

    def policy(self):
        return DEFAULT_POLICY


@dataclass(frozen=True)
class SubagentResult:
    """`_run_subagent_impl`'s return shape: the scrubbed answer plus the
    nested run's own `total_tokens`/`total_cost_usd`, needed so the calling
    `run_subagent` tool can fold them into the PARENT turn's live budget via
    `Command(update={"subagent_spend": [...]})` (see app/agent/graph.py's
    `State`/`should_continue`, GRAPH_PATTERNS.md pattern 46's disclosed
    "spend isn't live-folded" gap)."""

    answer: str
    total_tokens: int
    total_cost_usd: float


async def _run_subagent_impl(
    subagent_name: str,
    task: str,
    config: RunnableConfig,
    *,
    domain: str = "ecorp",
    registry: dict[str, tuple] | None = None,
    tools_by_name: Mapping[str, Any] | None = None,
    llm: Any = None,
    use_cache: bool = False,
) -> SubagentResult:
    """Build and run one nested, isolated agent turn, return its final answer
    plus its own usage. See GRAPH_PATTERNS.md pattern 46 for the full design.

    `registry`/`tools_by_name`/`llm` are DI for tests (mirrors
    `build_graph(deps=...)`'s override shape): default to the real
    `_SUBAGENT_REGISTRY`, every Ecorp tool by name, and a real ChatOpenAI
    client respectively. `tools_by_name` matters because a domain-scoped
    subagent's registry entry can name a tool that isn't in Ecorp's own
    `TOOLS` — looking it up there would silently drop it;
    `make_domain_subagent_tool` passes the calling domain's own tool objects.

    `domain` is only a cache-key/tracing label — it does NOT select
    `registry` (always whatever the caller passes, or `_SUBAGENT_REGISTRY`).

    `use_cache` opts into reusing a compiled nested graph (`_subagent_graph_cache`
    above) — not inferred from whether `llm`/`registry`/`tools_by_name` were
    passed, since the real production closures always pass their own
    registry/tools_by_name and that inference would silently defeat caching
    for every non-Ecorp domain. Only the two real `run_subagent` tool
    closures pass `use_cache=True`; tests never set it, so they're
    automatically excluded.

    Isolation, in one place: a FRESH `messages` list (subagent's own system
    prompt + the delegated `task` as its sole HumanMessage, never this
    conversation's history); the subagent's own tool subset and model alias;
    SecurityCtx inherited unconditionally from `config`; its own smaller
    fixed budget (MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN); a throwaway MemorySaver, never the
    durable checkpointer — bounded to complete within this one tool call.

    Uses `build_subagent_graph()` — build_graph()'s same node
    functions/factories via `_assemble_shared_graph_parts`, but a leaner
    one-shot topology: no semantic-cache read/write (a subagent's answer can
    never cross-serve to/from a top-level query), no suggest_followups, no
    history compaction (unreachable given MAX_SUBAGENT_TOKENS_PER_RUN). See
    that function's docstring for the full per-node reasoning.
    """
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx):
        return SubagentResult(_NO_CTX_REFUSAL, 0, 0.0)

    reg = registry if registry is not None else _SUBAGENT_REGISTRY
    entry = reg.get(subagent_name)
    if entry is None:
        return SubagentResult(f"No subagent named {subagent_name!r} is registered.", 0, 0.0)
    record, resolved_tool_names = entry
    all_tools_by_name = tools_by_name if tools_by_name is not None else {t.name: t for t in TOOLS}
    nested_tools = [all_tools_by_name[name] for name in resolved_tool_names if name in all_tools_by_name]

    # Deferred imports: app/agent/graph.py imports tools.py at module level
    # for TOOL_CAPABILITIES/TOOLS, and manifest.py imports graph.py at its
    # own module level — importing either back here at module level would
    # close a real import cycle. Only needed at call time regardless.
    from app.agent.graph import (
        MAX_SUBAGENT_ITERATIONS,
        MAX_SUBAGENT_TOKENS_PER_RUN,
        GraphDeps,
    )
    from app.agent.graph_build_subagent import build_subagent_graph
    from app.agent.manifest import AgentManifest
    from app.agent.usage_ledger import record_usage

    # Computed fresh every call regardless of caching below — cheap, fully
    # determined by record.system_prompt, no need to cache separately.
    nested_system_prompt = f"{record.system_prompt}\n\n{_CITATION_MARKER_WARNING}"

    cache_key = (domain, subagent_name)
    if use_cache and cache_key in _subagent_graph_cache:
        nested_graph = _subagent_graph_cache[cache_key]
    else:
        nested_llm = llm if llm is not None else tools_module.ChatOpenAI(
            model=record.model or CHAT_MODEL,
            base_url=OPENAI_API_BASE,
            api_key=SecretStr(OPENAI_API_KEY),
            temperature=0,
            stream_usage=True,
        ).bind_tools(nested_tools)

        nested_manifest = AgentManifest(name=record.name, system_prompt=nested_system_prompt)
        nested_domain = _SubagentDomainPlugin(nested_tools)

        nested_graph = build_subagent_graph(
            deps=GraphDeps(llm=nested_llm),
            manifest=nested_manifest,
            domain=nested_domain,
            max_iterations=MAX_SUBAGENT_ITERATIONS,
            max_tokens_per_turn=MAX_SUBAGENT_TOKENS_PER_RUN,
            max_cost_usd_per_turn=MAX_SUBAGENT_COST_USD_PER_RUN,
            # This function's own post-run check needs a genuinely empty final
            # AIMessage to detect a safety-net exit and report its own
            # "budget_exceeded" outcome — no_answer would otherwise fill it first.
            emit_no_answer_message=False,
        )
        if use_cache:
            _subagent_graph_cache[cache_key] = nested_graph

    parent_thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    nested_thread_id = f"{parent_thread_id}:subagent:{record.name}:{uuid.uuid4().hex[:8]}"
    nested_config = {
        "configurable": {"thread_id": nested_thread_id, "ctx": ctx},
        # Threads the parent's tracing callbacks (e.g. Langfuse) through, so
        # this run's internal LLM/tool calls show up as child spans instead
        # of being invisible until _invoke() returns.
        "callbacks": config.get("callbacks"),
        # Tags every event with which subagent it came from — consumed by
        # runtime_stream.py::_run_graph_stream to keep this run's "agent" node
        # stream from leaking into the client's main answer stream (both
        # graphs share that node name), and to surface tagged tool activity.
        "metadata": {
            "subagent_name": record.name,
            "parent_thread_id": parent_thread_id,
            "domain": domain,
        },
        # LangGraph's own graph-step cap — coarser than MAX_SUBAGENT_ITERATIONS
        # (an agent-node count): pre/post-loop nodes and 2-step tool round
        # trips mean runtime.py's flat "12" undercounts here and trips
        # GraphRecursionError early (see
        # test_respects_its_own_smaller_iteration_ceiling_not_the_parents).
        # Derived from MAX_SUBAGENT_ITERATIONS with margin so it can't drift.
        "recursion_limit": MAX_SUBAGENT_ITERATIONS * 2 + 15,
    }

    # No auto-seeding here (unlike runtime.py::_ensure_seeded_async) — this
    # graph runs exactly once, so the system prompt is just the first message.
    #
    # `nested_graph.ainvoke` awaited directly via `_arun_with_timeout`, not a
    # separate `asyncio.run(...)`: this function is `async def`, called on
    # the SAME loop the top-level graph's nodes run on, so LangGraph's sync
    # Pregel loop never has to see this graph's async-only node factories
    # (which a plain `.invoke()` would raise "No synchronous function
    # provided" against).
    started = time.monotonic()
    try:
        result_state = await _arun_with_timeout(
            nested_graph.ainvoke,
            {
                "messages": [
                    SystemMessage(content=nested_system_prompt),
                    HumanMessage(content=task),
                ],
                "require_approval": False,
            },
            config=nested_config,
            _timeout_seconds=SUBAGENT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        metrics.agent_subagent_run_total.labels(subagent=record.name, outcome="timeout").inc()
        metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(
            time.monotonic() - started
        )
        raise
    except Exception:
        metrics.agent_subagent_run_total.labels(subagent=record.name, outcome="error").inc()
        metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(
            time.monotonic() - started
        )
        raise
    duration = time.monotonic() - started

    total_tokens = result_state.get("total_tokens", 0)
    total_cost_usd = result_state.get("total_cost_usd", 0.0)
    messages = result_state.get("messages", [])
    final_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    content = final_ai.content if final_ai is not None else ""
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()
    # A missing/empty final answer means should_continue's "__end__" fired
    # from one of its safety-net checks (max iterations/tokens/cost, no-
    # progress — pattern 10) before reaching check_output. Rather than
    # re-deriving which budget tripped, treat "no answer" as one robust signal.
    if content:
        answer = scrub(content)
        outcome = "completed"
    else:
        answer = (
            f"Subagent {record.name!r} did not produce a final answer before "
            "hitting one of its own safety budgets."
        )
        outcome = "budget_exceeded"

    await record_usage(ctx, nested_thread_id, record.model or CHAT_MODEL, total_tokens)

    metrics.agent_subagent_run_total.labels(subagent=record.name, outcome=outcome).inc()
    metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(duration)
    logger.info(
        "subagent_completed",
        extra={
            "subagent_name": record.name,
            "thread_id": nested_thread_id,
            "outcome": outcome,
            "iterations": result_state.get("iterations", 0),
            "total_tokens": total_tokens,
            "duration_ms": round(duration * 1000, 1),
        },
    )
    return SubagentResult(answer, total_tokens, total_cost_usd)


if _SUBAGENT_REGISTRY:
    # Closed, dynamically-built enum — same idiom as Topic/Department — so
    # the LLM sees the full subagent menu directly in run_subagent's JSON
    # schema, no discovery round trip needed. Unlike skill_search/use_skill,
    # no separate list tool: a one-line description is small enough to embed
    # directly, unlike a skill's full body.
    #
    # Built once, at import time, not lazily like skills.py's get_skills() —
    # a compile-time enum must exist before any tool call can validate
    # against it. A subagent added to disk after the process starts needs a
    # restart to appear, same as TOOLS.
    SubagentName = Enum(  # type: ignore[misc]  # mypy can't infer members from a
        # dict comprehension — genuinely dynamic, built from whatever's on disk.
        "SubagentName", {name: name for name in sorted(_SUBAGENT_REGISTRY)}, type=str
    )

    _SUBAGENT_MENU = "\n".join(
        f"- {name}: {record.description}"
        for name, (record, _tools) in sorted(_SUBAGENT_REGISTRY.items())
    )

    class RunSubagentArgs(BaseModel):
        subagent_name: SubagentName = Field(
            ..., description=f"Which subagent to delegate to. Options:\n{_SUBAGENT_MENU}"
        )
        task: str = Field(
            ...,
            description="The self-contained task to delegate. The subagent has NO "
            "access to this conversation's history, so include everything it needs "
            "to know in this one description.",
        )
        # Injected by ToolNode, never shown to the LLM. Must be declared on
        # the args_schema itself (LangChain's InjectedToolCallId detection
        # scans args_schema fields, unlike `config: RunnableConfig` which
        # scans the raw function) — omitting it raises TypeError at
        # invocation. Needed to build the ToolMessage returned via Command().
        tool_call_id: Annotated[str, InjectedToolCallId]

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=RunSubagentArgs)
    async def run_subagent(
        subagent_name: SubagentName,
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
        result = await _run_subagent_impl(
            subagent_name.value, task, config, domain="ecorp", use_cache=True
        )
        return Command(
            update={
                "messages": [ToolMessage(content=result.answer, tool_call_id=tool_call_id)],
                "subagent_spend": [(result.total_tokens, result.total_cost_usd)],
            }
        )

    # Inserted right after skill_search/use_skill (index 2), the same
    # leading-tier position skill_tools_first gives run_subagent for every
    # other domain (see that function's docstring, and its caveat that this
    # placement isn't independently live-verified). No shared-helper route
    # here since TOOLS has no separate "reused tools" list.
    TOOLS.insert(2, run_subagent)
