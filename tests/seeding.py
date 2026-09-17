"""`seed_thread(graph, thread_id)` (2026-09-16) — call this before the FIRST
`graph.ainvoke()`/`.astream()` on any thread a test builds itself via
`build_graph()` directly. Shared by `tests/live/` and `tests/deepeval/`
(both drive `build_graph()` in-process), so it lives here rather than in
either package's own conftest.

A real, disclosed finding from actually running one of these tests, not
assumed: `app/agent/graph.py`'s own `agent()` node docstring says the base
SYSTEM_PROMPT "is seeded once per thread by
app/agent/runtime.py::_ensure_seeded_async before the graph ever runs, so
we don't repeat it here" — every production path (API, Telegram,
agent-worker) goes through that seeding via `runtime.py`'s own
`astream_events_turn`/`_unattended`, but a test that calls
`build_graph().ainvoke()` directly, bypassing `runtime.py` entirely, never
triggers it. Caught live: a test asking "What are Ecorp support hours?"
against a graph built this way got an EMPTY first response, a synthetic
retry nudge, then a tool call computing `24 * 7 = 168` and a fabricated
"Ecorp is open 24/7, 168 support hours a week" answer — the model had ZERO
tool-routing guidance, only each tool's own individual docstring (which
LangChain always sends regardless of SystemMessage presence). This means
every test in `tests/live/`/`tests/deepeval/`, and `scripts/eval.py`'s own
release-gating golden dataset, had been exercising an UN-GUIDED agent this
whole time, not the one real users actually get — reused
`app.agent.runtime._ensure_seeded_async` directly rather than re-deriving
the seeding logic (it reads `graph.manifest.system_prompt`, the correct
prompt for whatever domain `graph` was actually built for, and is
safe/idempotent to call before every turn, not just the first).
"""
from app.agent.runtime import _ensure_seeded_async


async def seed_thread(graph, thread_id: str) -> None:
    """Seed `thread_id` with `graph`'s own system prompt before the first
    real turn — see this module's own docstring for why this is required
    for a test that calls `build_graph().ainvoke()`/`.astream()` directly.
    Thin wrapper, not a reimplementation: delegates straight to
    `app.agent.runtime._ensure_seeded_async`, the exact function every
    production path already relies on, so this can never drift out of
    sync with what real users actually get."""
    await _ensure_seeded_async(graph, thread_id)
