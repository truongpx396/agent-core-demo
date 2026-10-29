"""Generic infrastructure helpers with no dependency on any node's own logic:
`instrumented` (structured lifecycle logging wrapper applied at graph-registration time),
`friendly_tool_error` (ToolNode's error-recovery formatter), `make_llm` (the production
`ChatOpenAI` client factory) and `without_tools` (the same client with the tool schemas taken off).

`make_llm` takes the model, endpoint and key as arguments, defaulting to the configured ones. A
caller that wants a different endpoint (a test pointing the graph at a local Ollama) passes it:
`GraphDeps(llm_factory=functools.partial(make_llm, model=..., base_url=...))`. There is no module
global to monkeypatch.
"""
import asyncio
import functools
import logging
import time

import psycopg
from langchain_core.runnables import RunnableBinding
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphBubbleUp
from pydantic import SecretStr

from app.core.config import CHAT_MODEL, OPENAI_API_BASE, OPENAI_API_KEY, settings
from app.core.tool_idempotency import MutatingToolTimedOut
from app.core.tool_schema import strict_tool_schema

logger = logging.getLogger(__name__)


def instrumented(node_name: str):
    """Wrap a node with structured start/complete/failed/paused lifecycle logs.

    Applied once, at registration (build_graph), to every node, never hand-rolled inside one, so the logged
    field set can't drift and plain node functions stay directly callable from tests.

    Logs carry node name, run_id, outcome and duration_ms, NEVER message content or the state dict, which
    would be a second, unscrubbed, non-expiring copy of prompt/document text outside Langfuse
    (GRAPH_PATTERNS.md pattern 14).

    `human_approval`'s `interrupt()` raises `GraphInterrupt` (a `GraphBubbleUp`) to pause the run: normal
    control flow, logged as `node_paused` and re-raised untouched, not `node_failed`.

    Async-aware: nodes that call an LLM or hit Redis/Qdrant/ml-service (`agent`, `compact_history`,
    `suggest_followups`, `check_semantic_cache`, `retrieve_context`, `write_semantic_cache`, `moderate_input`)
    are `async def`, so their I/O waits on the event loop instead of occupying LangChain's shared default
    executor (`min(32, cpu_count+4)` threads): raising this app's concurrency to 50 barely moved throughput
    until that executor stopped being the bottleneck. Every other node stays sync `def` (pure in-memory logic,
    per LangGraph's guidance). Detected via `asyncio.iscoroutinefunction(fn)`, not a flag.
    """

    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(state, *args, **kwargs):
                run_id = state.get("run_id", "-") if isinstance(state, dict) else "-"
                logger.info("node_started", extra={"node": node_name, "run_id": run_id})
                start = time.monotonic()
                try:
                    result = await fn(state, *args, **kwargs)
                except GraphBubbleUp:
                    logger.info(
                        "node_paused", extra={"node": node_name, "run_id": run_id}
                    )
                    raise
                except Exception as exc:
                    logger.warning(
                        "node_failed",
                        extra={
                            "node": node_name,
                            "run_id": run_id,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                            "error_class": type(exc).__name__,
                        },
                    )
                    raise
                logger.info(
                    "node_completed",
                    extra={
                        "node": node_name,
                        "run_id": run_id,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
                return result

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            run_id = state.get("run_id", "-") if isinstance(state, dict) else "-"
            logger.info("node_started", extra={"node": node_name, "run_id": run_id})
            start = time.monotonic()
            try:
                result = fn(state, *args, **kwargs)
            except GraphBubbleUp:
                logger.info(
                    "node_paused", extra={"node": node_name, "run_id": run_id}
                )
                raise
            except Exception as exc:
                logger.warning(
                    "node_failed",
                    extra={
                        "node": node_name,
                        "run_id": run_id,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                        "error_class": type(exc).__name__,
                    },
                )
                raise
            logger.info(
                "node_completed",
                extra={
                    "node": node_name,
                    "run_id": run_id,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                },
            )
            return result

        return wrapper

    return decorator


def friendly_tool_error(error: Exception) -> str:
    """Turned into a ToolMessage by ToolNode's handle_tool_errors instead of
    propagating and killing the run — the agent sees this on its next turn.

    `MutatingToolTimedOut` (app/core/tool_idempotency.py) gets its own
    message: a plain "try a different approach" is actively dangerous
    advice for a mutating/outward tool's soft timeout, since the
    underlying write may well have already committed — "try again" here
    means "retry with a brand new tool_call_id," which is exactly the one
    case nothing keyed on tool_call_id can catch. Steering the agent to
    verify first is the general defense, since it doesn't depend on which
    tool this was.

    ToolNode calls this with only the exception (`handler(e)` —
    `langgraph.prebuilt.tool_node._handle_tool_error`), never the tool
    call itself, which is why this dispatches on the exception's TYPE
    rather than on a tool name/capability lookup.
    """
    if isinstance(error, MutatingToolTimedOut):
        return (
            f"{error.tool_name} timed out, but its effect may already have been "
            "applied on the far side of that timeout before this failure was "
            "reported. Do NOT blindly call it again with the same arguments — "
            "a retry would run under a brand new call id, invisible to this "
            "app's own duplicate-call detection. First check whether it already "
            "happened, using a read-only tool for this domain (e.g. a status/"
            "list tool), and only retry if it clearly did not."
        )
    if isinstance(error, psycopg.Error):
        # The driver's message carries the failing statement ("LINE 1: ... FROM support_tickets t LEFT
        # JOIN support_ti...") and names tables and columns. The model repeats what it is told to the
        # person, so that went to the user's screen (Langfuse trace `e0ca1456`). Only the class goes;
        # the full message stays on the tool's own span for whoever runs the system. And "try a
        # different approach" is wrong advice for a store that rejected a correct request.
        return (
            f"Tool failed ({type(error).__name__}): the data store rejected a valid request, so this is a fault "
            "on the service's side and not in your arguments. Do not retry it or try another query. Tell the "
            "person plainly that this lookup is unavailable right now."
        )
    return f"Tool failed ({type(error).__name__}: {error}). Try a different approach."


# What `bind_tools` puts on a client; `without_tools` takes exactly these back off.
_TOOL_KWARGS = frozenset({"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"})


def without_tools(llm):
    """`llm` with the tool schemas `make_llm` bound taken off, for a call that has no use for tools.

    `make_llm` binds the domain's tools to the one client every node is given. A node that sends a
    single prompt and wants plain text back (the follow-up suggestions, the history summary) used to
    send all of them too: 15 schemas, 2,869 prompt tokens, for a one-sentence prompt (Langfuse trace
    `475b073a`). That is cost on every such call, and a prefix of its own that shares nothing with the
    chat calls', so the server reads it cold: 15 of that turn's 40 seconds, after the answer was done.
    A model offered tools may also answer a follow-up request with a tool call. Any other binding
    (a per-client keyword) is kept; a client that is not a binding is returned unchanged."""
    if not isinstance(llm, RunnableBinding):
        return llm
    kept = {k: v for k, v in llm.kwargs.items() if k not in _TOOL_KWARGS}
    inner = without_tools(llm.bound)
    return inner.bind(**kept) if kept else inner


def make_llm(
    tools: list,
    *,
    model: str = CHAT_MODEL,
    base_url: str = OPENAI_API_BASE,
    api_key: str = OPENAI_API_KEY,
    strict: bool | None = None,
):
    # `strict=None` follows TOOL_CALLS_STRICT. The tools are bound as dicts only when strict, so the off position is the
    # exact call this function made before the setting existed.
    use_strict = settings.tool_calls_strict if strict is None else strict
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=SecretStr(api_key),
        temperature=0,
        # astream_events (app/agent/runtime/stream.py's astream_events_turn) forces this
        # model through its streaming code path even though agent() calls
        # .invoke() — OpenAI-compatible streaming only includes token usage
        # in the final chunk when explicitly requested, so without this,
        # response.usage_metadata is silently None under --stream mode:
        # MAX_TOKENS_PER_TURN never trips, and Langfuse shows 0 tokens.
        stream_usage=True,
    ).bind_tools([strict_tool_schema(t) for t in tools] if use_strict else tools)
    # No `parallel_tool_calls=False` here (removed 2026-09-09): genuine multi-tool-call turns are supported.
    #
    # History: two simultaneous tool calls used to come back from litellm's ollama_chat provider as ONE
    # malformed tool_calls entry with concatenated ids/names/args (Langfuse `fc0a31db`/`dbd2c02b`,
    # 2026-09-08). `parallel_tool_calls=False` looked like a fix but was a proven no-op (ollama_chat doesn't
    # support it; litellm silently drops it): the corruption recurred (`3c6ed3b0`).
    #
    # Root cause was in litellm: OllamaChatCompletionResponseIterator's chunk_parser restarts its tool-call
    # index at 0 per chunk instead of per response, so two calls in separate chunks both get index 0 and any
    # OpenAI-compatible client string-concatenates them. Fixed at that layer
    # (deploy/litellm/patches/sitecustomize.py, loaded via PYTHONPATH in docker-compose.yml) with a globally
    # increasing index.
    #
    # The flag was then removed rather than kept as defense in depth: approval and routing already handled the
    # full tool_calls list, just untested with a real batch, now verified live
    # (tests/live/test_agent_parallel_tool_calls.py). A batch needing approval is approved or rejected as ONE
    # decision covering every call, trading granularity for round trips, not visibility.
