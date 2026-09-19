"""Generic infrastructure helpers with no dependency on any node's own
logic: `_instrumented` (structured lifecycle logging wrapper applied at
graph-registration time), `_friendly_tool_error` (ToolNode's
error-recovery formatter), `_make_llm` (the production `ChatOpenAI`
client factory), and `_content_words` (a crude word-overlap helper
shared by `graph.py`'s vague-query check and `graph_routing.py`'s
citation-grounding check). Split out of `app/agent/graph.py` for file
size (see `graph_hitl.py`/`graph_routing.py`/`graph_tools.py`/
`graph_skills.py` for sibling splits); no behavior change.

`_content_words` is read back into `graph.py`'s `_retrieval_query` via a
deferred import to avoid a circular import.

`_make_llm` reads `CHAT_MODEL`/`OPENAI_API_BASE`/`OPENAI_API_KEY` through
`graph_module.X` rather than a static bare import — several `tests/live/*`
files monkeypatch these as attributes on the live `graph` module object,
which a bare `from X import Y` would miss (copies the reference once, at
import time). Same fix as `graph_hitl.py`'s `graph_module.interrupt`/
`STATE_SCHEMA_VERSION`.
"""
import asyncio
import functools
import logging
import re
import time

from langchain_openai import ChatOpenAI
from langgraph.errors import GraphBubbleUp
from pydantic import SecretStr

from app.agent import graph as graph_module
from app.agent.tools import TOOLS

logger = logging.getLogger(__name__)


def _instrumented(node_name: str):
    """Wrap a node with structured start/complete/failed/paused lifecycle logs.

    Applied once, at graph-registration time (build_graph), to every node
    — never hand-rolled inside a node function — so the logged field set
    can't drift, and the plain node functions stay directly callable from
    tests unwrapped.

    Logs carry node name, run_id, outcome, and duration_ms — NEVER message
    content or the state dict, which would create a second, unscrubbed,
    non-expiring copy of prompt/document text outside Langfuse's tracing
    (GRAPH_PATTERNS.md pattern 14).

    `human_approval`'s `interrupt()` raises `GraphInterrupt` (a
    `GraphBubbleUp`) to pause the run — normal control flow, logged as
    `node_paused` and re-raised untouched, not caught as `node_failed`.

    Async-aware: nodes making a real LLM call or hitting Redis/Qdrant/
    ml-service (`agent`, `compact_history`, `suggest_followups`,
    `check_semantic_cache`, `retrieve_context`, `write_semantic_cache`,
    `moderate_input`) are `async def` so their I/O waits on the event loop
    instead of occupying a slot in LangChain's shared, process-wide
    default executor (`min(32, cpu_count+4)` threads) — raising this
    app's own concurrency limits to 50 barely moved throughput until this
    was fixed, because that shared executor was the next bottleneck.
    Every other node stays plain sync `def`, per LangGraph's own guidance,
    since they're pure in-memory logic with nothing to await. Detected via
    `asyncio.iscoroutinefunction(fn)`, not a caller flag.
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


def _friendly_tool_error(error: Exception) -> str:
    """Turned into a ToolMessage by ToolNode's handle_tool_errors instead of
    propagating and killing the run — the agent sees this on its next turn."""
    return f"Tool failed ({type(error).__name__}: {error}). Try a different approach."


def _make_llm(tools: list = TOOLS):
    return ChatOpenAI(
        model=graph_module.CHAT_MODEL,
        base_url=graph_module.OPENAI_API_BASE,
        api_key=SecretStr(graph_module.OPENAI_API_KEY),
        temperature=0,
        # astream_events (app/agent/runtime_stream.py's astream_events_turn) forces this
        # model through its streaming code path even though agent() calls
        # .invoke() — OpenAI-compatible streaming only includes token usage
        # in the final chunk when explicitly requested, so without this,
        # response.usage_metadata is silently None under --stream mode:
        # MAX_TOKENS_PER_TURN never trips, and Langfuse shows 0 tokens.
        stream_usage=True,
    ).bind_tools(tools)
    # No `parallel_tool_calls=False` here (removed 2026-09-09) — genuine
    # multi-tool-call turns are fully supported now.
    #
    # History: two simultaneous tool calls used to come back from litellm's
    # ollama_chat provider as ONE malformed tool_calls entry with
    # concatenated ids/names/args (Langfuse `fc0a31db`/`dbd2c02b`,
    # 2026-09-08). `parallel_tool_calls=False` was added as an apparent fix
    # but was a proven no-op (ollama_chat doesn't support the param;
    # litellm silently drops it) — confirmed when the corruption recurred
    # later (`3c6ed3b0`).
    #
    # Root cause was in litellm: OllamaChatCompletionResponseIterator's
    # chunk_parser restarts its tool-call index at 0 per chunk instead of
    # per response, so two calls in separate chunks both get index 0 and
    # get string-concatenated by any OpenAI-compatible client. Fixed at
    # that layer (litellm-patches/sitecustomize.py, loaded via PYTHONPATH
    # in docker-compose.yml) to hand out a globally-increasing index.
    #
    # `parallel_tool_calls=False` was then removed rather than kept as
    # defense-in-depth: the approval/routing code already handled the full
    # tool_calls list generically, just untested with a real batch — now
    # verified live (tests/live/test_agent_parallel_tool_calls.py). A batch
    # needing approval is approved/rejected as ONE decision covering every
    # call, trading round trips for granularity, not visibility.


# A small, deliberately crude stopword list — good enough to stop common
# words from inflating an overlap ratio, without pulling in an NLP
# dependency for what's fundamentally a coarse, directional heuristic.
_STOPWORDS = frozenset(
    "a an the is are was were and or of to in for on at by with from that "
    "this it its be as you your can will if not into their his her they "
    "he she we".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _content_words(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2
    }
