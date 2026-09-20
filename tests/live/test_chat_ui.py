"""Browser E2E against the built-in web UI (app/api/static/index.html) —
the one client this app ships that a Python-level test can't drive, since
its whole job is rendering a real browser's `fetch()`-based SSE parsing
(see that file's own module comment: "no build step, no CDN dependency").
Needs `real_stack` (tests/live/conftest.py): a real uvicorn + agent-worker
pointed at real Postgres/Redis/Qdrant/Ollama, since the page actually calls
`POST /chat/stream/queued` and `POST /chat/resume` (see its own `send()`/
`resume()`) — both real only through the real Redis queue + a real
agent-worker process, never in-process.

Generous timeouts throughout (`RESPONSE_TIMEOUT_MS`): a small, real,
CPU-bound model genuinely takes longer per turn than this suite's usual
sub-second fake-LLM tests, and a mutating-tool turn below waits through TWO
full model round trips (the initial tool-call decision, then the
post-approval synthesis of the tool result into a final answer).
`RESPONSE_TIMEOUT_MS` must stay comfortably ABOVE
`tests/live/conftest.py::real_stack`'s own `REQUEST_TIMEOUT_SECONDS`
override, or this suite's own assertion times out first and reports a
misleading "element never appeared" instead of the real app-level
`[error: Request exceeded Ns timeout]` text that actually explains a slow
turn — verified directly: a real CI run measured a single real
`qwen2.5:1.5b` tool-calling turn taking 67.7s end to end.

The agentic-feature tests below (citations, a skill, a subagent) use
`real_stack_with_retrieval` instead of `real_stack` — the one difference
is a REAL embedding model + seeded Qdrant (see that fixture's own
docstring), needed because `search_docs`/`skill_search` are both
Qdrant-backed and return nothing real against `real_stack`'s deliberately
unembedded setup. Each names its tool/skill/subagent explicitly in the
prompt, same low-risk posture as the calculator/remember tests above —
this suite is about proving the real integration works end to end, not
about testing whether a 1.5B model can infer intent on its own.
`MULTI_STEP_RESPONSE_TIMEOUT_MS` is wider still: a skill or subagent turn
chains multiple real model calls (skill_search -> use_skill -> the
skill's own tool calls, or the parent's tool-call decision -> a full
NESTED subagent turn -> the parent's synthesis of that result), not just
the one/two RESPONSE_TIMEOUT_MS already budgets for.
"""
import pytest

# `pytest.importorskip`, not a plain top-level import: `playwright` is only
# installed for `test-live`'s own CI job/`make test-live` (see
# requirements-dev.txt) — the fast `test` job never installs it, correctly,
# since it never runs anything e2e-marked. But pytest still IMPORTS every
# test module during COLLECTION regardless of which markers end up
# selected/deselected, so a plain `from playwright.sync_api import ...`
# would break collection (not just this file's own tests) the moment this
# module is even discovered in an environment without playwright — caught
# directly in CI. `importorskip` here instead marks the whole module
# skipped, exactly the "self-skip, don't fail" contract this module's own
# tests already extend to a missing Docker daemon (see tests/containers.py).
playwright_sync_api = pytest.importorskip("playwright.sync_api")
Page = playwright_sync_api.Page
expect = playwright_sync_api.expect

pytestmark = pytest.mark.e2e

RESPONSE_TIMEOUT_MS = 210_000
MULTI_STEP_RESPONSE_TIMEOUT_MS = 300_000


def _send(page: Page, text: str) -> None:
    page.fill("#input", text)
    page.click("#send")


def test_sending_a_message_streams_a_real_answer_via_the_calculator_tool(page: Page, real_stack: str):
    page.goto(real_stack)
    expect(page.locator("#messages")).to_be_visible()

    _send(page, "what is 21 * 2? Use the calculator tool.")

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_contain_text("42", timeout=RESPONSE_TIMEOUT_MS)


def test_a_mutating_tool_call_pauses_for_approval_and_resumes_on_approve(page: Page, real_stack: str):
    """Drives the REAL approve/reject round trip
    (`renderApprovalButtons`/`resume()` → `POST /chat/resume`) — the flow
    README.md's own "Built-in web UI" section still (incorrectly, as of
    this test) describes as unbuilt. Uses `remember`, not `add_note`: both
    are mutating tools gated by the same mandatory human_approval pause
    (GRAPH_PATTERNS.md pattern 15), but `remember` needs no `topic` value
    the small model might get wrong, keeping this test's real, imperfect
    tool-argument generation as low-risk as this scenario allows.
    """
    page.goto(real_stack)

    _send(page, "Remember that I prefer dark roast coffee. Use the remember tool.")

    approve_button = page.get_by_role("button", name="Approve")
    expect(approve_button).to_be_visible(timeout=RESPONSE_TIMEOUT_MS)
    approve_button.click()

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_be_visible(timeout=RESPONSE_TIMEOUT_MS)
    expect(approve_button).not_to_be_visible()


def test_a_read_only_tool_call_returns_real_data(page: Page, real_stack: str):
    """query_employees, not calculator — proves the browser round trip
    works for a tool whose result comes from a real Postgres row
    (postgres-init/02-appdata.sql's seeded `employees` table), not just
    arithmetic the model could plausibly fake. Read-only (TOOL_CAPABILITIES),
    so no approval pause — needs no `real_stack_with_retrieval`, unlike the
    tests below."""
    page.goto(real_stack)

    _send(page, "Who is Ecorp's Support Lead? Use the query_employees tool.")

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_contain_text("Dana Whitfield", timeout=RESPONSE_TIMEOUT_MS)


def test_a_grounded_answer_shows_a_real_citation(page: Page, real_stack_with_retrieval: str):
    """search_docs against a REAL embedding + a REAL seeded Qdrant
    (real_stack_with_retrieval, backed by scripts/seed.py's sample docs) —
    proves retrieval and the citations UI (`renderCitations`,
    app/api/static/index.html) work end to end, not just that search_docs
    returns SOMETHING. "Ecorp: Support Hours" (scripts/sample_docs.py) is
    the source doc; its own text is asserted, not just marker presence, so
    a citation box rendering with the WRONG source still fails this test.
    """
    page.goto(real_stack_with_retrieval)

    _send(page, "What are Ecorp's support hours? Use the search_docs tool.")

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_contain_text("9am", timeout=RESPONSE_TIMEOUT_MS)
    citation = page.locator(".citations .citation-item").first
    expect(citation).to_be_visible()
    expect(citation).to_contain_text("Support Hours")


def test_a_skill_is_found_and_followed(page: Page, real_stack_with_retrieval: str):
    """skill_search -> use_skill -> the skill's own instructed tool calls
    (GRAPH_PATTERNS.md pattern 45) — `expense-summary` (skills/expense-
    summary/SKILL.md) chosen specifically because its OWN instructions
    only need `calculator` afterward, not search_docs/query_employees too:
    skill_search itself still needs real embeddings (real_stack_with_retrieval),
    but this keeps the number of real, independently-fallible model
    decisions in one test to the minimum that still proves the skill
    pattern genuinely works (found -> loaded -> followed), not just that
    calculator alone still works (already covered above)."""
    page.goto(real_stack_with_retrieval)

    _send(
        page,
        "I have two expenses to report: coffee $5 and lunch $12. "
        "Use skill_search first to find the right skill for summarizing "
        "expenses, then follow it.",
    )

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_contain_text("17", timeout=MULTI_STEP_RESPONSE_TIMEOUT_MS)


def test_a_subagent_delegates_and_returns_a_real_answer(page: Page, real_stack_with_retrieval: str):
    """run_subagent (GRAPH_PATTERNS.md pattern 46) — delegates to the
    bundled `researcher` subagent (subagents/researcher/AGENT.md, visible
    on the default Ecorp domain this test already runs against), which
    itself calls query_employees in a NESTED graph run this conversation
    never sees directly. Asserting the real employee name in the PARENT
    turn's own final answer is what actually proves delegation completed
    and its result was folded back in, not just that run_subagent was
    invoked. real_stack_with_retrieval is used for its real embedding
    model, not because this specific task needs search_docs — `researcher`
    also offers search_docs, and keeping ONE fixture for every agentic
    test here (rather than a THIRD variant with an embedding model but no
    seeded data) is the simpler, still-correct choice."""
    page.goto(real_stack_with_retrieval)

    _send(
        page,
        "Use the researcher subagent to look up who Ecorp's Support Lead is.",
    )

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_contain_text("Dana Whitfield", timeout=MULTI_STEP_RESPONSE_TIMEOUT_MS)
