"""Generic infrastructure helpers with no dependency on any particular
node's own logic: `_instrumented` (the structured lifecycle-logging
wrapper every node gets at graph-registration time), `_friendly_tool_error`
(ToolNode's error-recovery formatter), `_make_llm` (the production
`ChatOpenAI` client factory), and `_content_words` (a crude word-overlap
helper reused by three unrelated call sites — `app/agent/graph.py`'s own
`_retrieval_query` vague-query check and `app/agent/graph_routing.py`'s
citation-grounding check — so it belongs here, not with either caller or
either sibling guardrail split). Split out of `app/agent/graph.py` purely
for file size — see that module's own docstring, and
`app/agent/graph_hitl.py`/`app/agent/graph_routing.py`/
`app/agent/graph_tools.py`/`app/agent/graph_skills.py`'s for the sibling
splits. No behavior change from the pre-split single-file version.

`_content_words` is read back into `app/agent/graph.py`'s `_retrieval_query`
via a deferred (function-body-local) import, to avoid a real circular
import — same pattern `_assemble_shared_graph_parts` already uses for
`check_output`/`should_continue`/`_make_llm` itself.

`_make_llm` reads `CHAT_MODEL`/`OPENAI_API_BASE`/`OPENAI_API_KEY` through
`graph_module.X` rather than a plain statically-imported bare name — same
real bug/fix as `app/agent/graph_hitl.py`'s own `graph_module.interrupt`/
`graph_module.STATE_SCHEMA_VERSION` (see its docstring): several
`tests/live/*` files do `monkeypatch.setattr(graph_module, "CHAT_MODEL", ...)`
/`"OPENAI_API_BASE"`, patching an attribute on the live `app.agent.graph`
module object — a statically-imported bare name here would bind to the
ORIGINAL values once, at this module's own import time, permanently, so
the monkeypatch would silently never take effect. `OPENAI_API_KEY` isn't
currently patched anywhere, but is module-qualified too for consistency —
cheap insurance against the same bug resurfacing if a future test needs to.
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

    Applied once, at graph-registration time (see build_graph), to every
    node — never hand-rolled inside a node function — so the logged field
    set can't drift by which node's author remembered to add it, and so the
    plain node functions stay directly callable from tests exactly as
    before (see this module's docstring): only the graph-registered copy is
    wrapped, the module-level name is untouched.

    Logs carry the node name, run_id, outcome, and duration_ms — NEVER
    message content or the state dict. A node dumping `state` into a log
    would create a second, unscrubbed, non-expiring copy of prompt/document
    text sitting outside Langfuse's tracing, which is where that data is
    meant to live (see GRAPH_PATTERNS.md pattern 14).

    `human_approval`'s `interrupt()` raises `GraphInterrupt` (a
    `GraphBubbleUp`) to pause the run — that's normal control flow, not a
    failure, so it's logged as `node_paused` and re-raised untouched rather
    than caught as `node_failed`.

    Async-aware: a handful of nodes (`agent`, `compact_history`,
    `suggest_followups`, `check_semantic_cache`, `retrieve_context`,
    `write_semantic_cache` — the ones that make a real LLM call or hit
    Redis/Qdrant) are `async def`, so their real I/O waits on the event
    loop instead of occupying a slot in LangChain's shared, process-wide
    default executor (`langchain_core.runnables.config.run_in_executor`,
    `min(32, os.cpu_count()+4)` threads total — verified directly against
    that source, and directly measured here: raising this app's own
    AGENT_WORKER_MAX_CONCURRENCY/CHECKPOINTER_POOL_MAX_SIZE to 50 barely
    moved throughput on a 50-concurrent-turn burst until this was fixed,
    because that shared executor was the next thing every turn queued
    behind). Every other node stays plain sync `def` — deliberately, per
    LangGraph's own guidance: they're pure in-memory/regex logic with
    nothing to await, and forcing them async would just add executor-hop
    overhead for zero benefit (`check_output`/`retry_output` in particular
    are hit by ~100 existing direct-call unit tests each — see
    tests/agent/test_nodes.py — that a real I/O node wouldn't have,
    reinforcing that these were never the nodes worth converting). Detected
    via `asyncio.iscoroutinefunction(fn)`, not a caller-supplied flag, so a
    node's own definition (`def` vs `async def`) is the only place this
    ever needs to be decided.
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
        # astream_events (app/agent/runtime.py's astream_events_turn) forces this
        # model through its streaming code path even though agent() calls
        # .invoke() — OpenAI-compatible streaming only includes token usage
        # in the final chunk when explicitly requested, so without this,
        # response.usage_metadata is silently None under --stream mode:
        # MAX_TOKENS_PER_TURN never trips, and Langfuse shows 0 tokens.
        stream_usage=True,
    ).bind_tools(tools)
    # No `parallel_tool_calls=False` here (removed 2026-09-09) — genuine
    # multi-tool-call turns are now supported end to end, deliberately.
    #
    # History: a real, live-verified streaming bug once made two
    # simultaneous tool calls in one turn (e.g. calculator + add_note)
    # come back as ONE malformed tool_calls entry whose id/name/arguments
    # were each the raw concatenation of both calls' own fields
    # ("calculatoradd_note" glued into one string, both calls' JSON args
    # glued into one unparseable blob) — burning ~15k tokens across 5
    # identical failed retries before ever reaching a human approval pause
    # (Langfuse trace `fc0a31db`/`dbd2c02b`, 2026-09-08). `parallel_tool_calls
    # =False` was added here as the apparent fix, but it was a proven no-op
    # against this stack: litellm's ollama_chat provider doesn't list
    # `parallel_tool_calls` in its get_supported_openai_params() at all, and
    # litellm-config.yaml's `drop_params: true` makes litellm silently
    # discard unsupported params instead of erroring, so the parameter
    # never reached Ollama — confirmed when the exact same corruption
    # recurred in a fresh trace (`3c6ed3b0`, 2026-09-09) well after that
    # line shipped.
    #
    # The actual bug lived one layer down, in litellm itself:
    # OllamaChatCompletionResponseIterator.chunk_parser builds a fresh
    # Delta per top-level Ollama stream chunk, and Delta's own
    # auto-indexing restarts its counter at 0 for every chunk instead of
    # tracking it across the whole response — so two tool calls arriving
    # in separate chunks (how ollama_chat actually delivers them) both got
    # index 0, and any OpenAI-compatible client (langchain_openai
    # included) is spec-correct to merge same-index tool_call chunks by
    # string-concatenating their fields, which is exactly the glued
    # garbage observed. Fixed at that layer:
    # litellm-patches/sitecustomize.py, loaded into the litellm proxy
    # container via PYTHONPATH (docker-compose.yml) — it patches
    # chunk_parser to hand out one globally-increasing index per response
    # instead of per chunk. See that file for the full writeup.
    #
    # With the transport bug actually fixed, `parallel_tool_calls=False`
    # was removed rather than kept as "harmless defense-in-depth": this
    # app's should_continue/human_approval/ToolNode/runtime.py SSE/frontend
    # code was already written generically over the full tool_calls list
    # (see _mandatory_gate_reason, _reject_tool_calls, human_approval's own
    # interrupt payload) even though nothing had ever exercised it with a
    # real multi-call batch — verified live end to end (calculator +
    # add_note in one turn: routes to human_approval with both calls
    # bundled, one approve/reject resumes both, ToolNode runs both, mixed
    # success/failure is reported per-call) and covered by
    # tests/live/test_agent_parallel_tool_calls.py. Deliberate consequence,
    # not an oversight: a batch needing approval is now approved/rejected
    # as ONE decision covering every call in it, not one decision per call
    # — the pending-tool-calls list in the approval prompt (human_approval,
    # the `approval_required` SSE event, the web UI's renderApprovalButtons)
    # already shows every call in the batch, so this trades "one action per
    # approval" for "fewer round trips," not for reduced visibility into
    # what's being approved.


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
