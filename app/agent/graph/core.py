"""The turn-entry gating nodes and the per-node reliability policy.

`validate_input` stamps the turn (`ctx` from config, the per-turn resets) and is the one entry point
of every `graph.invoke()`; `route_after_validation` fails closed on a bad `ctx` (`reject_context`)
before looking at the message (`reject_input`). `moderate_input` / `route_after_moderation` /
`reject_moderation` screen the text before anything that spends (pattern 25). `route_after_compaction`
/ `context_window_exceeded` end a turn whose history summary outgrew its budget. `AGENT_RETRY_POLICY`
is the retry policy for the one node that talks to the model (pattern 7).

Nodes and routers live at module level (not nested in build_graph) so they're unit-testable directly,
without compiling a graph or calling a real LLM. Where things are:
- `graph/state.py` — `State`, `STATE_SCHEMA_VERSION` and the safety-budget constants (a leaf)
- `graph/build.py` — `GraphDeps`, `GraphProfile` and `build_graph`, the composition root
- `graph/routing.py` — `should_continue`, `check_output`, `route_after_check`
- `graph/agent_node.py`, `retrieval.py`, `cache.py`, `compaction.py`, `followups.py`, `retry.py`,
  `hitl.py` — one node (or node family) each
- `graph/tool_guards.py`, `loop_guards.py`, `skill_guards.py`, `output_guardrails.py`,
  `citations.py` — the checks the gates are built from
- `graph/messages.py`, `graph/utils.py` — helpers with no node logic
"""
import functools
import logging
import os
import subprocess
import uuid
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import RetryPolicy, default_retry_on

from app.agent.graph.messages import (
    CANNOT_HELP_MESSAGE,
    heal_legacy_retry_artifacts,
    human_has_content,
    human_text,
    last_human_message,
)
from app.agent.graph.state import (
    MAX_HISTORY_SUMMARY_CHARS,
    STATE_SCHEMA_VERSION,
    State,
)
from app.core import gateway, metrics, moderation
from app.core.security import valid_ctx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

def _retry_agent_on(exc: Exception) -> bool:
    """LangGraph's own predicate, minus what repeating cannot change. LangGraph's default retries
    everything that is not a programming error and only excludes `httpx.HTTPStatusError` 4xx, but the
    OpenAI SDK's status errors are not that class, so a rejected key, an unknown model, a 422 and a
    content-guardrail 400 were all retried 3 times: seconds of backoff and more refused calls in the
    gateway's log before the caller learned anything (Langfuse trace `b376e4d8`). The gateway's budget
    stop repeats until the budget resets, so it is refused a retry whatever status it came with
    (app/core/gateway.py)."""
    return (
        not gateway.is_budget_exceeded(exc)
        and not gateway.is_permanent_client_error(exc)
        and default_retry_on(exc)
    )


# Reliability policy for `agent` (see build_graph): retry a transient
# LLM-endpoint failure (connection error, 5xx, 429, 408) before giving up. LangGraph's
# default retry_on excludes programming errors, so this can't mask a real
# bug as a flaky call (pattern 7); `_retry_agent_on` adds the failures
# that are never worth retrying (a budget stop, a permanent 4xx).
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
        "output_findings": {},
        "last_retry_reason": None,
        "retry_reason_repeat_count": 0,
        "cache_hit": False,
        "moderation_blocked": False,
        # The routing decision is per turn (route_model sets it, after moderation and the cache). Reset here so a refused or
        # cache-hit turn never carries the last turn's `strong` into its own accounting, and never on a resume (which skips this node).
        "model_route": None,
        "followups": [],
    }
    # A thread that had a retry before retry artifacts were tagged still holds the feedback as an
    # untagged HumanMessage. Tagged here, the first time the thread is used again, every reader
    # (the model's view, the summary, the transcript) treats it as the graph's, not the person's.
    healed = heal_legacy_retry_artifacts(state.get("messages") or [])
    if healed:
        metrics.agent_legacy_retry_artifacts_healed_total.inc(len(healed))
        updates["messages"] = healed
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
            AIMessage(content=CANNOT_HELP_MESSAGE)
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
