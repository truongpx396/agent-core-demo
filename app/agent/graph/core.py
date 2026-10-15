"""Enhanced LangGraph agent demonstrating practical patterns beyond a basic
LLM+tools loop: input validation/moderation, pre-fetched RAG context framed
as untrusted data (`<retrieved_document>` delimiters + a system rule that
delimited text is data, never instructions), bounded history (see
HISTORY_TOKEN_CEILING/_FLOOR, never splitting a tool_call/ToolMessage
pair), an output quality gate with retry, per-node reliability policies
(retrieve_context degrades, agent gets AGENT_RETRY_POLICY, tool exceptions
become a message instead of crashing), opt-in human-in-the-loop
(app/channels/chat.py's `--hitl`), built-in parallel tool execution via
ToolNode, per-node telemetry (instrumented), multi-tenant isolation via
SecurityCtx (stamped once by validate_input, fails closed at
reject_context, every downstream read/write pre-filtered by it), and
cross-session memory (automatic recall, explicit-only write via
`remember`). See GRAPH_PATTERNS.md for the full pattern catalog.

Nodes/routing functions live at module level (not nested in build_graph)
so they're unit-testable directly, without compiling a graph or calling a
real LLM. `agent`/`retrieve_context` are the exception — they need an
injected client, so they're built by factories (make_agent_node,
make_retrieve_context_node); see GraphDeps/build_graph.

`State`, `STATE_SCHEMA_VERSION` and the safety-budget constants live in the
leaf module `graph/state.py`. This file holds `SYSTEM_PROMPT`, the
turn-entry gating nodes (validate_input/moderate_input/reject_*/
context_window_exceeded), the swappable defaults `default_search`/
`default_cache_get`/`default_cache_set` (kept here because
tests/conftest.py monkeypatches them on this module, and graph/build.py
reads them via `graph_module.X`), and `GraphDeps`/
`assemble_shared_graph_parts` (the composition root build_graph()/
build_subagent_graph() both use). Everything else was split out purely for
file size (no behavior change):
- graph/messages.py — human-message text helpers
- graph/compaction.py — token estimation, history trimming, make_compact_history_node
- graph/cache.py — semantic-cache node pair
- graph/retrieval.py — make_retrieve_context_node
- graph/agent_node.py — make_agent_node
- graph/followups.py — make_suggest_followups_node
- graph/retry.py — retry_output, make_retry_exhausted_node, make_no_answer_fallback_node
- graph/routing.py — should_continue, check_output, route_after_check (+ its own sibling splits)
- graph/hitl.py / graph/skill_guards.py / graph/tool_guards.py / graph/utils.py /
  graph/build.py / graph/build_subagent.py — pre-existing splits (see each one's docstring)
"""
import functools
import logging
import os
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

# `interrupt` is unused here directly (moved to graph/hitl.py's
# human_approval) but re-exported deliberately: graph/hitl.py reads it as
# `graph_module.interrupt` so tests' `monkeypatch.setattr(graph,
# "interrupt", ...)` keep working. Don't remove this import.
from langgraph.types import RetryPolicy, default_retry_on, interrupt  # noqa: F401

from app.agent.graph.messages import (
    human_has_content,
    human_text,
    last_human_message,
)
from app.agent.graph.state import (
    MAX_HISTORY_SUMMARY_CHARS,
    MAX_ITERATIONS,
    MAX_TOKENS_PER_TURN,
    STATE_SCHEMA_VERSION,
    State,
)
from app.core import gateway, metrics, moderation

# OPENAI_API_BASE/OPENAI_API_KEY: same re-export reasoning as `interrupt`
# above — graph/utils.py's `make_llm` and tests/live/* monkeypatches read
# them live off this module. Don't remove.
from app.core.config import (  # noqa: F401
    CHAT_MODEL,
    MAX_COST_USD_PER_TURN,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
)
from app.core.security import SecurityCtx, valid_ctx
from app.retrieval import semantic_cache

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import builtin as tools

logger = logging.getLogger(__name__)

def _retry_agent_on(exc: Exception) -> bool:
    """LangGraph's own predicate, minus the gateway's budget stop. That refusal repeats until the
    budget resets, so a retry cannot succeed: it only delays the caller's error by the backoff and
    adds refused calls to the gateway's log (app/core/gateway.py)."""
    return not gateway.is_budget_exceeded(exc) and default_retry_on(exc)


# Reliability policy for `agent` (see build_graph): retry a transient
# LLM-endpoint failure (connection error, 5xx) before giving up. LangGraph's
# default retry_on excludes programming errors, so this can't mask a real
# bug as a flaky call (pattern 7); `_retry_agent_on` adds the one failure
# that is never worth retrying.
AGENT_RETRY_POLICY = RetryPolicy(max_attempts=3, retry_on=_retry_agent_on)



@functools.cache
def _graph_version() -> str:
    """Identifies the build that wrote a checkpoint, for resumability_error's
    cross-restart check. `GRAPH_VERSION` (env var) is what a real deployment
    sets at build/deploy time; the git SHA is a dev-time convenience
    fallback; "unknown" if neither is available. Never raises — this is
    metadata, not a safety budget, so it fails open the same way
    MAX_TOKENS_PER_TURN's usage_metadata lookup does.

    Cached for the process: the build doesn't change under a running
    process, and `validate_input` calls this every turn. Uncached, a
    deployment that sets no `GRAPH_VERSION` forked `git` on every turn (and
    paid a failed exec when the image has no git)."""
    version = os.environ.get("GRAPH_VERSION")
    if version:
        return version
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - best-effort dev convenience only
        return "unknown"


# --- Node: validate input. Fixed entry point for every graph.invoke() call
# (START -> validate_input), but NOT re-run when resuming a paused HITL turn
# (Command(resume=...) resumes inside human_approval directly) — so this
# runs exactly once per turn, which is what resetting iterations/
# total_tokens/run_id here requires (otherwise they'd persist across turns
# and MAX_ITERATIONS would fire on an arbitrary future turn). History
# trimming/summarization stays out of this node (compact_history, which runs
# after moderation and a cache miss) since it needs an LLM call and this stays a plain, dependency-free
# function of state/config.
#
# `ctx` is read from config["configurable"]["ctx"] (never state or message
# content) and stamped ONCE, here — the ONLY node that writes state["ctx"];
# everything else reads it read-only. route_after_validation fails closed
# on an invalid one before retrieve_context ever runs.
def validate_input(state: State, config: RunnableConfig) -> dict:
    updates: dict = {
        "iterations": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        # `None` = the reducer's explicit reset marker (see concat_or_reset);
        # writing `[]` through a concatenating reducer would be a no-op.
        "subagent_spend": None,
        # A cancel on one turn must not decide the routing of a LATER turn's
        # approval: `route_after_approval` tests `cancelled` before `approved`,
        # and nothing else clears either field. Reset here, never on a resume
        # (a resume re-enters inside human_approval and skips this node).
        "cancelled": False,
        "approved": False,
        "run_id": uuid.uuid4().hex[:8],
        "graph_version": _graph_version(),
        "state_schema_version": STATE_SCHEMA_VERSION,
        "ctx": (config.get("configurable") or {}).get("ctx"),
        # Reset every turn — a rejected/short-circuited turn (reject_input,
        # reject_context) must never leak a PRIOR turn's citations into
        # the streamed `citations` SSE event.
        "citations": [],
        # Same reason: `context` and its anchor are set only by retrieve_context, which a
        # cache-hit turn never reaches, so they'd otherwise carry over from the last turn.
        "context": "",
        "context_anchor_index": None,
        "used_citations": [],
        "ungrounded_claims_count": 0,
        "likely_uncited_citations": [],
        "likely_misattributed_citations": [],
        "deferred_instead_of_acting": False,
        "fabricated_tool_output": False,
        "skipped_required_tool": None,
        "leaks_system_prompt": False,
        "last_retry_reason": None,
        "retry_reason_repeat_count": 0,
        "cache_hit": False,
        "moderation_blocked": False,
        "followups": [],
    }
    return updates


def route_after_validation(
    state: State,
) -> Literal["moderate_input", "reject_input", "reject_context"]:
    """Checks ctx first, then the message. A missing/malformed ctx routes
    to reject_context (a system-level failure), kept distinct from
    reject_input's "you typed nothing" so an infra problem doesn't read as
    a user error in the transcript or in agent_requests_total's outcome
    label (see runtime/stream.py::_turn_outcome).

    A valid turn goes straight to moderate_input, and compaction runs
    LATER, only on a semantic-cache miss (route_after_cache). Compaction is
    an LLM call (a summary of the trimmed turns), so it follows pattern
    25's "moderation before any spend": a screened-out turn and a cache-hit
    turn must not pay for a summarization. The cost of that order is that
    history only compacts on turns that reach the model; a long run of
    cache hits defers it to the next miss, which is when it is needed.
    """
    if not valid_ctx(state.get("ctx")):
        return "reject_context"
    last_human = last_human_message(state["messages"])
    if not human_has_content(last_human):
        return "reject_input"
    return "moderate_input"


# --- Node: input moderation (GRAPH_PATTERNS.md pattern 25) — runs BEFORE
# the semantic cache lookup or retrieval, so a screened-out input never
# reaches either. ---
async def moderate_input(state: State) -> dict:
    """Screens the TEXT only (`human_text`) — app/core/moderation.py is a pattern
    screen + ML classifier over that text, neither can inspect an image's
    actual content. An image-only message has empty `human_text`, so it
    passes through unblocked — a disclosed gap (pattern 44): this app
    screens WORDS, not PIXELS.

    async: moderation.screen awaits a real HTTP call to ml-service.
    """
    last_human = last_human_message(state["messages"])
    if last_human is None:
        return {"moderation_blocked": False}
    result = await moderation.screen(human_text(last_human))
    return {"moderation_blocked": not result.allowed}


def route_after_moderation(
    state: State,
) -> Literal["reject_moderation", "check_semantic_cache"]:
    return "reject_moderation" if state.get("moderation_blocked") else "check_semantic_cache"


def reject_moderation(state: State) -> dict:
    """Screened out by moderate_input — short-circuits BEFORE the
    semantic cache, retrieval, or any LLM call, same as reject_input/
    reject_context above it in the turn."""
    return {
        "messages": [
            AIMessage(
                content="I can't help with that request."
            )
        ]
    }


def reject_input(state: State) -> dict:
    # AIMessage, not HumanMessage: the *system* is producing this text,
    # not the user.
    return {
        "messages": [
            AIMessage(content="I didn't receive a question — please try again.")
        ]
    }


def reject_context(state: State) -> dict:
    """Fail closed on a missing/malformed SecurityCtx — before any
    retrieval, any tool call, any spend. See route_after_validation."""
    metrics.agent_missing_ctx_total.inc()
    return {
        "messages": [
            AIMessage(
                content="I couldn't verify who's asking, so I can't help with "
                "this request. Please try again."
            )
        ]
    }


def route_after_compaction(
    state: State,
) -> Literal["retrieve_context", "context_window_exceeded"]:
    """AR-015a's edge case as a NAMED terminal state, not silent
    truncation or an ever-growing prompt: if compact_history's updated
    `history_summary` is still over MAX_HISTORY_SUMMARY_CHARS, end the
    turn explicitly here rather than let agent() inject a runaway summary
    on every future call for the rest of the thread's life."""
    if len(state.get("history_summary") or "") > MAX_HISTORY_SUMMARY_CHARS:
        metrics.agent_context_window_exceeded_total.inc()
        return "context_window_exceeded"
    return "retrieve_context"


def context_window_exceeded(state: State) -> dict:
    """Terminal node for route_after_compaction's over-budget branch — a
    dead-end conversation, not a crash: the thread's history_summary grew
    past MAX_HISTORY_SUMMARY_CHARS even after compact_history just tried
    to shrink it, so this ends the turn with an explicit message rather
    than keep compacting into an unbounded loop or silently truncating
    the summary (which would quietly drop whatever fell off the end)."""
    return {
        "messages": [
            AIMessage(
                content="This conversation has grown too long to continue safely "
                "in this thread, even after compacting earlier turns — please "
                "start a new conversation."
            )
        ]
    }


async def default_cache_get(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]] | None:
    return await semantic_cache.get(ctx, query)


async def default_cache_set(ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict]) -> None:
    await semantic_cache.set(ctx, query, answer, citations)


async def default_search(query: str, ctx: SecurityCtx | None) -> tuple[str, list[dict]]:
    """Thin wrapper over `tools.gather_context` matching the
    `Callable[[str, SecurityCtx | None], Awaitable[tuple[str, list[dict]]]]`
    shape `make_retrieve_context_node` expects, so tests can pass a plain
    fake function to the factory.

    `ctx` flows straight through — the same value search_docs/remember read
    from config when the *model* calls them as tools, so pre-fetched and
    on-demand retrieval are policy-enforced identically. Hybrid search
    (dense+sparse RRF, cross-encoder reranked) and cross-session memory
    recall both live inside `gather_context` (see app/agent/tools/builtin.py, pattern 20).

    async: gather_context awaits real I/O (the reranker is an HTTP call to
    ml-service, not local ONNX compute).
    """
    return await tools.gather_context(ctx, query)


@dataclass
class GraphDeps:
    """Bundle of the graph's swappable external clients, built once at the
    composition root (build_graph) and threaded into the node factories
    that need them — see make_agent_node, make_retrieve_context_node.
    Unset fields fall back to the real clients inside build_graph(); tests
    set individual fields to inject fakes instead of monkeypatching module
    globals (see tests/agent/graph/test_agent_node.py, tests/agent/graph/test_nodes.py).
    """

    llm: Any = None
    # The LiteLLM alias `llm` is bound to, used only to price its calls
    # (app/agent/cost/pricing.py). None = the global chat alias; a delegated run whose
    # specialist declares its own model sets it so its calls are priced by that
    # model, not by the parent's.
    model_alias: str | None = None
    # The usage-event `kind` this client's calls are recorded under (app/agent/cost/usage_events.py).
    # "chat" for the main loop; a delegated run sets "subagent" so its spend is separable.
    meter_kind: str = "chat"
    search_docs: (
        Callable[[str, "SecurityCtx | None"], Awaitable[tuple[str, list[dict]]]] | None
    ) = None
    cache_get: (
        Callable[["SecurityCtx | None", str], Awaitable[tuple[str, list[dict]] | None]] | None
    ) = None
    cache_set: (
        Callable[["SecurityCtx | None", str, str, list[dict]], Awaitable[None]] | None
    ) = None


@dataclass(frozen=True)
class _SharedGraphParts:
    """What `build_graph()` and `build_subagent_graph()` both need before
    they diverge — see `assemble_shared_graph_parts`'s own docstring."""

    deps: GraphDeps
    manifest: "AgentManifest"
    domain_tools: list
    tool_capabilities: Mapping[str, str]
    llm_client: Any
    agent: Callable
    retrieve_context: Callable
    domain_should_continue: Callable
    domain_check_output: Callable
    domain_invalid_tool_call: Callable


def assemble_shared_graph_parts(
    deps: GraphDeps | None,
    manifest: "AgentManifest",
    domain: "DomainPlugin",
    max_iterations: int | None,
    max_tokens_per_turn: int | None,
    max_cost_usd_per_turn: float | None,
    enforce_citations: bool = True,
) -> _SharedGraphParts:
    """Setup shared by `build_graph()`/`build_subagent_graph()` before they
    diverge on topology: resolves `deps`/`manifest`/`domain` defaults,
    computes the domain's tool set/capabilities, builds the LLM client, the
    `agent`/`retrieve_context` closures, and the `should_continue`/
    `check_output` partials bound to this domain's capability mapping and
    budget ceiling. Factored out so both builders duplicate only their
    actual topology (which nodes exist, how they're wired) — "one
    pipeline, not two that can drift" (pattern 46). Each caller builds
    whatever else it additionally needs from `.llm_client`/`.deps`.

    `enforce_citations=False` (only `build_subagent_graph`) drops the two
    places a graph pushes `[n]` markers on the model: the tail "every sentence
    must end with its marker" reminder in `agent`, and `check_output`'s
    uncited/misattributed findings and marker insertion. A nested run's
    sources are not the parent's, so its prompt says to use none.
    """
    # Deferred: graph/routing.py and graph/utils.py both import State/
    # constants back from THIS module at their own top level, so importing
    # them here at module level would close a real cycle. Only needed at
    # call time. make_agent_node/make_retrieve_context_node don't strictly
    # need deferring either, but kept together for one single import block.
    from app.agent.graph.agent_node import make_agent_node
    from app.agent.graph.retrieval import make_retrieve_context_node
    from app.agent.graph.routing import check_output, should_continue
    from app.agent.graph.tool_guards import invalid_tool_call
    from app.agent.graph.utils import make_llm

    deps = deps or GraphDeps()

    domain_tools = domain.tools()
    if manifest.allowed_tools:
        allowed = set(manifest.allowed_tools)
        domain_tools = [t for t in domain_tools if t.name in allowed]
    domain_tool_capabilities = domain.tool_capabilities()
    domain_valid_tool_names = frozenset(t.name for t in domain_tools)

    llm_client = deps.llm or make_llm(domain_tools)
    agent = make_agent_node(
        llm_client,
        model_alias=deps.model_alias,
        kind=deps.meter_kind,
        citation_reminder=enforce_citations,
    )
    retrieve_context = make_retrieve_context_node(deps.search_docs or default_search)
    # A plain module-level function (not a factory) bound to this domain's
    # capability mapping via functools.partial — see should_continue's own
    # docstring for why it stays a directly-callable module-level function
    # rather than becoming a fifth factory in this file.
    domain_should_continue = functools.partial(
        should_continue,
        tool_capabilities=domain_tool_capabilities,
        valid_tool_names=domain_valid_tool_names,
        max_iterations=max_iterations if max_iterations is not None else MAX_ITERATIONS,
        max_tokens=max_tokens_per_turn if max_tokens_per_turn is not None else MAX_TOKENS_PER_TURN,
        max_cost_usd=max_cost_usd_per_turn if max_cost_usd_per_turn is not None else MAX_COST_USD_PER_TURN,
    )
    # Same "plain function, not a factory" shape — bound to THIS domain's
    # own system_prompt, not the bare Ecorp SYSTEM_PROMPT, so
    # reveals_system_prompt checks an answer against the prompt it was
    # ACTUALLY seeded with.
    domain_check_output = functools.partial(
        check_output, system_prompt=manifest.system_prompt, enforce_citations=enforce_citations
    )
    # Same shape again: name the bad tools against the set should_continue routed with, not
    # Ecorp's (see invalid_tool_call).
    domain_invalid_tool_call = functools.partial(
        invalid_tool_call, valid_tool_names=domain_valid_tool_names
    )

    return _SharedGraphParts(
        deps=deps,
        manifest=manifest,
        domain_tools=domain_tools,
        tool_capabilities=domain_tool_capabilities,
        llm_client=llm_client,
        agent=agent,
        retrieve_context=retrieve_context,
        domain_should_continue=domain_should_continue,
        domain_check_output=domain_check_output,
        domain_invalid_tool_call=domain_invalid_tool_call,
    )


