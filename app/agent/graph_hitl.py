"""Human-in-the-loop pause/resume: `human_approval`/`route_after_approval`
(the opt-in-or-mandatory approval gate, GRAPH_PATTERNS.md pattern 8/15/36)
and `resumability_error_async`/`_resumability_error_from_state`/
`CANCEL_SENTINEL` (safety checks before resuming a paused thread, pattern
16/36). Split out of `app/agent/graph.py` for file size (see
`graph_routing.py`/`graph_utils.py` for sibling splits); no behavior
change.

`interrupt` (in `human_approval`) and `STATE_SCHEMA_VERSION` (in
`_resumability_error_from_state`) are read via `graph_module.X` rather
than a static import — several tests monkeypatch both as attributes on
the live `graph` module object, which a bare `from X import Y` would
miss (copies the reference once, at import time). Same fix as
`graph_build.py`'s `graph_module._default_cache_get`/`_default_cache_set`.
"""
from typing import Literal, cast

from langchain_core.messages import AIMessage

from app.agent import graph as graph_module
from app.agent.graph import State
from app.agent.graph_tools import _reject_tool_calls
from app.core import metrics


def _resumability_error_from_state(state) -> str | None:
    """The actual check, factored out of graph/config-fetching so
    `resumability_error_async` stays a thin `aget_state` wrapper. (A sync
    counterpart was removed alongside its only caller, `scripts/hitl_demo.py`
    — see runtime.py's module docstring.) See `resumability_error_async`
    for the two failure modes this distinguishes.

    `state.next` ALONE does not mean "paused" — verified against a real
    race: it's truthy for any checkpoint written mid-run, not just one
    suspended at interrupt(). Trusting only `state.next` let a concurrent
    `Command(resume=...)`/cancel (pattern 43's `/chat/cancel`/
    `/chat/resume`, which can legitimately race an actively-streaming
    turn) start a SECOND, competing Pregel execution against the same
    checkpoint — reproduced directly: the second call drove the turn to
    completion silently while the first caller's `astream_events()` got no
    further tokens, and an unrelated resume value got consumed as ordinary
    input with no exception raised. Checking `state.tasks[i].interrupts`
    (the same signal `_run_graph_stream` reads for its `approval_required`
    event) is what actually distinguishes "paused" from "still running".
    """
    if not state.next or not any(task.interrupts for task in state.tasks):
        metrics.agent_checkpoint_issue_total.labels(reason="checkpoint_lost").inc()
        return (
            "checkpoint_lost: no paused run found for this thread — it may "
            "have completed, never existed, its checkpoint was lost, or "
            "it's still actively running (not yet paused at an approval gate)."
        )
    paused_schema = state.values.get("state_schema_version")
    if paused_schema != graph_module.STATE_SCHEMA_VERSION:
        metrics.agent_checkpoint_issue_total.labels(
            reason="checkpoint_incompatible"
        ).inc()
        return (
            f"checkpoint_incompatible: paused under state_schema_version "
            f"{paused_schema!r}, this build is {graph_module.STATE_SCHEMA_VERSION!r} — "
            "refusing to resume into a possibly different topology."
        )
    return None


async def resumability_error_async(graph, config: dict) -> str | None:
    """Check before every Command(resume=...) call — never resume blindly.
    Returns None if safe, otherwise a human-readable reason (and
    increments `agent_checkpoint_issue_total`).

    ASYNC ONLY — the only caller, `astream_events_resume`, runs directly on
    the checkpointer's own event loop, where only `graph.aget_state` (not
    the sync accessor) is safe to call. A sync counterpart existed for
    `scripts/hitl_demo.py`'s plain invoke-driven loop and was removed with
    that script.

    Two failure modes (mirroring the intel-agent names in GRAPH_PATTERNS.md):

    - **checkpoint_lost** — no paused run for this thread (`state.next` is
      empty): wrong thread id, run already completed/errored past the
      pause, or the checkpoint was lost/corrupted. Catches calling
      `Command(resume=...)` against nothing pending.
    - **checkpoint_incompatible** — checkpoint's `state_schema_version`
      differs from this build's. A renamed State key, removed node, or
      reordered edge could otherwise fail silently on resume, so resuming
      into a possibly different topology is refused. A differing
      `graph_version` (build SHA) alone is NOT an error — ordinary
      deploys change it without touching `STATE_SCHEMA_VERSION` (see that
      constant's docstring for the bump discipline this relies on).
    """
    return _resumability_error_from_state(await graph.aget_state(config))


CANCEL_SENTINEL = "cancelled"  # app/agent/runtime_stream.py::cancel_run resumes a paused run with this value


def human_approval(state: State) -> dict:
    """Pause the graph and ask a human to approve pending tool calls.

    `interrupt()` suspends execution here; the caller resumes with
    `graph.invoke(Command(resume=True_or_False_or_"cancelled"), config)`,
    at which point this node continues with that value. Three outcomes,
    not two (pattern 36): approved, rejected (model sees a ToolMessage
    and can react), and CANCELLED (`runtime_stream.py::cancel_run`) — a
    caller-initiated abort, deliberately distinct from rejection:
    `route_after_approval` sends a cancelled run straight to `__end__`,
    never back to `agent`.
    """
    last_ai = cast(AIMessage, state["messages"][-1])
    tool_calls = last_ai.tool_calls or []
    decision = graph_module.interrupt(
        {
            "action": "approve_tool_calls",
            "tool_calls": [
                {"name": tc["name"], "args": tc["args"]} for tc in tool_calls
            ],
        }
    )
    if decision == CANCEL_SENTINEL:
        metrics.agent_human_approval_total.labels(decision="cancelled").inc()
        rejections = _reject_tool_calls(
            tool_calls, "Cancelled by request — this action will not run."
        )
        return {"messages": rejections, "approved": False, "cancelled": True}
    if decision:
        metrics.agent_human_approval_total.labels(decision="approved").inc()
        return {"approved": True}
    metrics.agent_human_approval_total.labels(decision="rejected").inc()
    rejections = _reject_tool_calls(tool_calls, "Rejected by human reviewer.")
    return {"messages": rejections, "approved": False}


def route_after_approval(state: State) -> Literal["tools", "agent", "__end__"]:
    if state.get("cancelled"):
        # Cancellation ends the run outright — never back to `agent`, since
        # that would give the model a chance to react to a caller-initiated
        # abort rather than real feedback (pattern 36).
        return "__end__"
    return "tools" if state.get("approved") else "agent"
