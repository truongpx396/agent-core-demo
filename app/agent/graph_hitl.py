"""Human-in-the-loop pause/resume: `human_approval`/`route_after_approval`
(the opt-in-or-mandatory approval gate itself, GRAPH_PATTERNS.md pattern 8/
15/36) and `resumability_error_async`/`_resumability_error_from_state`/
`CANCEL_SENTINEL` (safety checks run before ever resuming a paused thread,
pattern 16/36). Split out of `app/agent/graph.py` purely for file size —
see that module's own docstring, and `app/agent/graph_routing.py`'s /
`app/agent/graph_utils.py`'s for the sibling splits. No behavior change
from the pre-split single-file version.

Two names below are read through `graph_module.X` rather than a plain
statically-imported bare name — `interrupt` (inside `human_approval`) and
`STATE_SCHEMA_VERSION` (inside `_resumability_error_from_state`) — because
tests monkeypatch BOTH as attributes on the live `app.agent.graph` module
object (`monkeypatch.setattr(graph, "interrupt", ...)` in several tests;
`monkeypatch.setattr("app.agent.graph.STATE_SCHEMA_VERSION", ...)` in
tests/agent/test_durable_checkpoint.py). A statically-imported bare name
would bind to the ORIGINAL object once, at this module's own import time,
permanently — Python's `from X import Y` copies the reference rather than
tracking X's attribute — so the monkeypatch would silently never take
effect here. Same real bug, same fix, as `app/agent/graph_build.py`'s own
`graph_module._default_cache_get`/`_default_cache_set` (see its own
comment for the first time this was caught).
"""
from typing import Literal, cast

from langchain_core.messages import AIMessage

from app.agent import graph as graph_module
from app.agent.graph import State
from app.agent.graph_tools import _reject_tool_calls
from app.core import metrics


def _resumability_error_from_state(state) -> str | None:
    """The actual check, factored out of graph/config-fetching so
    `resumability_error_async` stays a thin `await graph.aget_state(...)`
    wrapper around it — this used to also be shared with a SYNC
    `resumability_error` (graph.get_state, no await), removed alongside
    `scripts/hitl_demo.py`, its only caller (see
    app/agent/runtime.py's module docstring). See
    `resumability_error_async`'s docstring for the two failure modes this
    distinguishes.

    `state.next` ALONE is not enough to mean "paused" — verified
    empirically (a real race, reproduced against a live checkpointer):
    `state.next` is truthy for ANY checkpoint written mid-run, between two
    ordinary supersteps of a turn that's simply still executing, not
    suspended at an interrupt() at all. Checking only `state.next` let a
    concurrent `Command(resume=...)`/`Command(resume=CANCEL_SENTINEL)`
    (GRAPH_PATTERNS.md pattern 43's `POST /chat/cancel`/`/chat/resume`,
    which can legitimately race an ACTIVELY STREAMING — not yet paused —
    turn for the same thread_id) sail through as "safe," then actually
    start a SECOND, competing Pregel execution against the same
    checkpoint: reproduced directly — the second call silently drove the
    turn to completion through its own `ainvoke`, while the FIRST (real)
    caller's own `astream_events()` received zero further tokens, an
    unrelated CANCEL_SENTINEL/approval value got treated as ordinary
    continuation input since nothing was actually waiting to consume it,
    and no exception surfaced any of this. `state.tasks[i].interrupts` is
    the actual, specific signal `human_approval`'s `interrupt()` leaves
    behind (already what `_run_graph_stream` itself reads to build the
    `approval_required` event, `state.tasks[0].interrupts[0].value`) — so
    checking for at least one real pending interrupt, not just any
    pending task, is what actually distinguishes "genuinely paused" from
    "still running."
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
    Returns None if resuming is safe, otherwise a human-readable reason
    (and increments agent_checkpoint_issue_total, so this is visible in
    metrics rather than only to whichever caller happened to check).

    ASYNC ONLY — this app's only caller, `astream_events_resume`, runs
    directly ON the checkpointer's own event loop (via
    `init_graph_async()`), where only the checkpointer's async accessor
    (`graph.aget_state`) is safe to call; the sync one raises
    `asyncio.InvalidStateError` from that same loop (verified empirically
    against the original AsyncSqliteSaver, and the same loop-binding
    constraint holds for AsyncPostgresSaver). A sync counterpart
    (`resumability_error`) existed here for `scripts/hitl_demo.py`'s
    plain `graph.invoke`-driven pause/resume loop and was removed once
    that script was — see app/agent/runtime.py's module docstring.

    Two distinct failures, matching the two intel-agent names this mirrors
    (see the "Durable checkpointer" note in GRAPH_PATTERNS.md):

    - **checkpoint_lost** — no paused run exists for this thread
      (`state.next` is empty). This app has no separate durable-pointer /
      ephemeral-store split to name a *different* kind of loss — the
      checkpointer file itself is the durable store — so this is the
      practical equivalent: the thread id is wrong, the run already
      completed or errored past the pause, or (in a real deployment) the
      checkpoint file was deleted or corrupted. Calling
      `Command(resume=...)` against a thread with nothing pending is
      exactly the mistake this exists to catch before it happens.
    - **checkpoint_incompatible** — the checkpoint was written by a build
      whose `state_schema_version` differs from this build's
      `STATE_SCHEMA_VERSION`. A renamed State key, a removed node, or a
      reordered edge the paused thread might resume into can each fail
      *silently* and look like a clean run otherwise — resuming into a
      possibly different topology is refused instead. A differing
      `graph_version` (build SHA) ALONE is not an error: ordinary deploys
      change the SHA constantly without touching `STATE_SCHEMA_VERSION`,
      and treating that as fatal would make every deploy a resume-killer —
      see STATE_SCHEMA_VERSION's docstring for the bump discipline that
      keeps this distinction meaningful.
    """
    return _resumability_error_from_state(await graph.aget_state(config))


CANCEL_SENTINEL = "cancelled"  # app/agent/runtime.py::cancel_run resumes a paused run with this value


def human_approval(state: State) -> dict:
    """Pause the graph and ask a human to approve pending tool calls.

    `interrupt()` suspends execution here (LangGraph persists state via
    the checkpointer); the caller resumes with
    `graph.invoke(Command(resume=True_or_False_or_"cancelled"), config)`,
    at which point `interrupt()` returns that value and this node
    continues. Three outcomes, not two (GRAPH_PATTERNS.md pattern 36):
    approved, rejected (the model sees a ToolMessage and gets a chance to
    react — apologize, try something else), and CANCELLED
    (`app/agent/runtime.py::cancel_run`) — a caller-initiated abort, which is
    deliberately NOT the same as a rejection: `route_after_approval`
    sends a cancelled run straight to `__end__`, never back to `agent`,
    because cancellation means "stop this run," not "here's feedback for
    your next attempt."
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
        # Cancellation ends the run outright — never back to `agent`,
        # since that would give the model a chance to react to something
        # that's a caller-initiated abort, not feedback (see
        # human_approval's docstring, GRAPH_PATTERNS.md pattern 36).
        return "__end__"
    return "tools" if state.get("approved") else "agent"
