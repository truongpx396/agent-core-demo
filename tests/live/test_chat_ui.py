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

The agentic-feature tests below (the approved `remember` write, citations, a
skill, a subagent) use `real_stack_with_retrieval` instead of `real_stack` —
the one difference is a REAL embedding model + seeded Qdrant (see that
fixture's own docstring), needed because `search_docs`/`skill_search` are both
Qdrant-backed and return nothing real against `real_stack`'s deliberately
unembedded setup, and because `remember` embeds what it stores: against
`real_stack` it fails with a 404 for the embedding model, the model asks for it
again, and the second approval pause leaves the Approve button on screen (see
the `remember` test below). Each names its tool/skill/subagent explicitly in the
prompt, same low-risk posture as the calculator/remember tests above —
this suite is about proving the real integration works end to end, not
about testing whether a 1.5B model can infer intent on its own.
`MULTI_STEP_RESPONSE_TIMEOUT_MS` is wider still: a skill or subagent turn
chains multiple real model calls (skill_search -> use_skill -> the
skill's own tool calls, or the parent's tool-call decision -> a full
NESTED subagent turn -> the parent's synthesis of that result), not just
the one/two RESPONSE_TIMEOUT_MS already budgets for.
"""
import re
import time

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
# Comfortably above tests/live/conftest.py's own REQUEST_TIMEOUT_SECONDS
# (420s, tuned for a run_subagent turn's THREE chained real model round
# trips under this fixture's CPU-only Ollama container — see that
# constant's own comment) — otherwise this suite's own assertion would
# time out first and report a misleading "element never appeared" instead
# of the real, more informative `[error: Request exceeded 420s timeout]`.
MULTI_STEP_RESPONSE_TIMEOUT_MS = 600_000


# How long the page may take, once the turn has ended, to put the final text on screen. The turn's own
# length is what RESPONSE_TIMEOUT_MS / MULTI_STEP_RESPONSE_TIMEOUT_MS budget; this is only the render lag.
SETTLE_MS = 10_000

# The page's own two ways of saying "this turn is over": `finishTurn()` re-enables Send (done or error), and a
# pause for approval renders the Approve/Reject box (app/api/static/index.html: `send()` disables Send
# synchronously, so right after a click "enabled" really means "finished", not "not started yet").
_TURN_IS_OVER = "() => !document.getElementById('send').disabled || document.querySelector('.approval') !== null"


def _send(page: Page, text: str) -> None:
    page.fill("#input", text)
    page.click("#send")


def _answer_of_the_finished_turn(page: Page, timeout_ms: int = RESPONSE_TIMEOUT_MS):
    """The assistant's answer locator, returned the moment the turn has ENDED, not after a fixed wait.

    Asserting `to_contain_text(..., timeout=<the whole turn budget>)` on a live locator keeps polling for
    that whole budget even after the agent has stopped for good, so a turn that goes wrong costs the full
    timeout and says nothing about why. Real instance (a CI run of this suite, 2026-10-04): the model hit
    `human_approval` and paused at 13:41:40, nobody could answer, and `test_a_skill_is_found_and_followed`
    went on waiting for "17" until its 600 s limit expired at 13:52:49 (~11 minutes, about half of the
    whole `test-live` job). Waiting for the turn to END first makes that fail in the time the turn took,
    and a pause is reported as what it is, with the tool the agent was waiting to run.
    """
    page.wait_for_function(_TURN_IS_OVER, timeout=timeout_ms, polling=500)
    paused = page.locator(".approval .approval-question")
    if paused.count():
        pytest.fail(f"the turn paused for approval instead of answering: {paused.first.inner_text()}")
    return page.locator(".msg.assistant .answer-text").last


def test_sending_a_message_streams_a_real_answer_via_the_calculator_tool(page: Page, real_stack: str):
    page.goto(real_stack)
    expect(page.locator("#messages")).to_be_visible()

    _send(page, "what is 21 * 2? Use the calculator tool.")

    answer = _answer_of_the_finished_turn(page)
    expect(answer).to_contain_text("42", timeout=SETTLE_MS)


def test_a_mutating_tool_call_pauses_for_approval_and_resumes_on_approve(
    page: Page, real_stack_with_retrieval: str
):
    """Drives the REAL approve/reject round trip
    (`renderApprovalButtons`/`resume()` → `POST /chat/resume`) — the flow
    README.md's own "Built-in web UI" section still (incorrectly, as of
    this test) describes as unbuilt. Uses `remember`, not `add_note`: both
    are mutating tools gated by the same mandatory human_approval pause
    (GRAPH_PATTERNS.md pattern 15), but `remember` needs no `topic` value
    the small model might get wrong, keeping this test's real, imperfect
    tool-argument generation as low-risk as this scenario allows.

    Needs `real_stack_with_retrieval`, not `real_stack`: `remember` embeds the
    text it stores (`app/agent/tools.py::_remember_impl`), and `real_stack`
    runs with no real embedding model. Reproduced locally and read off the CI
    logs: the approved `remember` call failed immediately with the embedding
    model's 404, the 3B model reacted by asking for `remember` again, that
    second call paused for approval again, and the test then waited the full
    timeout for an Approve button that never went away — passing only on the
    runs where the model happened not to retry. With a real embedder the
    approved write succeeds and the turn ends after the one pause this test is
    about.
    """
    page.goto(real_stack_with_retrieval)

    _send(page, "Remember that I prefer dark roast coffee. Use the remember tool.")

    # The turn is over once it has paused (the Approve box is up) or ended without ever pausing. Only the
    # second is a failure, and waiting RESPONSE_TIMEOUT_MS for a button that can no longer appear is the
    # dead wait `_answer_of_the_finished_turn` exists to avoid.
    page.wait_for_function(_TURN_IS_OVER, timeout=RESPONSE_TIMEOUT_MS, polling=500)
    approve_button = page.get_by_role("button", name="Approve")
    expect(approve_button).to_be_visible(timeout=SETTLE_MS)
    approve_button.click()

    answer = page.locator(".msg.assistant .answer-text").last
    expect(answer).to_be_visible(timeout=RESPONSE_TIMEOUT_MS)
    # No explicit timeout here used to mean Playwright's 5s default — real
    # bug, caught live: `.answer-text` becomes visible as soon as the FIRST
    # streamed chunk arrives, not when the turn actually finishes; the
    # Approve button is only removed on the turn's terminal `done`/`error`
    # event (see index.html's own handleEvent), which can trail well behind
    # that first chunk once post-approval synthesis itself takes a while
    # (qwen2.5:3b under this fixture's CPU-only Ollama container — see
    # tests/live/conftest.py's own REQUEST_TIMEOUT_SECONDS comment). Always
    # racy in theory; only ever masked by how fast the old 1.5b model's
    # synthesis finished after that first chunk.
    expect(approve_button).not_to_be_visible(timeout=RESPONSE_TIMEOUT_MS)


def test_a_read_only_tool_call_returns_real_data(page: Page, real_stack: str):
    """query_employees, not calculator — proves the browser round trip
    works for a tool whose result comes from a real Postgres row
    (postgres-init/02-appdata.sql's seeded `employees` table), not just
    arithmetic the model could plausibly fake. Read-only (TOOL_CAPABILITIES),
    so no approval pause — needs no `real_stack_with_retrieval`, unlike the
    tests below."""
    page.goto(real_stack)

    _send(page, "Who is Ecorp's Support Lead? Use the query_employees tool.")

    answer = _answer_of_the_finished_turn(page)
    expect(answer).to_contain_text("Dana Whitfield", timeout=SETTLE_MS)


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

    answer = _answer_of_the_finished_turn(page)
    # A regex, not a literal "9am": real bug, caught live — the model's
    # OWN phrasing genuinely varies run to run ("9am" vs "9 AM" vs
    # "9:00 AM"), all equally correct answers a literal, case-sensitive
    # substring check would wrongly fail.
    expect(answer).to_contain_text(re.compile(r"9\s*am", re.IGNORECASE), timeout=SETTLE_MS)
    citation = page.locator(".citations .citation-item").first
    expect(citation).to_be_visible()
    expect(citation).to_contain_text("Support Hours")


# `advisory`: this test needs the 3B model to CHAIN several calls (skill_search -> use_skill -> calculator, or a
# delegation whose nested run must query correctly), and it does not reliably do that, however the test or the
# tool is worded (see GRAPH_PATTERNS.md pattern 48: a prompt change and a tool change each fixed one case and broke
# another; the CI record since the memory fix is 1 green run in 5). CI runs it in its own non-blocking step, so a
# model that stops halfway is a visible signal instead of a red gate, like deepeval.
@pytest.mark.advisory
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

    answer = _answer_of_the_finished_turn(page, MULTI_STEP_RESPONSE_TIMEOUT_MS)
    expect(answer).to_contain_text("17", timeout=SETTLE_MS)


# `advisory`: this test needs the 3B model to CHAIN several calls (skill_search -> use_skill -> calculator, or a
# delegation whose nested run must query correctly), and it does not reliably do that, however the test or the
# tool is worded (see GRAPH_PATTERNS.md pattern 48: a prompt change and a tool change each fixed one case and broke
# another; the CI record since the memory fix is 1 green run in 5). CI runs it in its own non-blocking step, so a
# model that stops halfway is a visible signal instead of a red gate, like deepeval.
@pytest.mark.advisory
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

    # The delegated task names the tool and the filter on purpose, the same way the sibling
    # test above says "Use the query_employees tool." What this test proves is DELEGATION (a
    # nested run completing and folding its result back), not whether qwen2.5:3b can work out
    # that a job title is only reachable through `department`: `query_employees` filters on
    # `department` and on NAME, so an unguided nested run searches the name column for
    # "Support Lead", gets "No matching employees found.", and answers that no such person
    # exists. That is what this test did on 8 consecutive CI runs (2026-09-21 onward), each
    # burning the full 10-minute timeout, until the task was made explicit.
    _send(
        page,
        "Use the researcher subagent to look up who Ecorp's Support Lead is. Tell it to use "
        "the query_employees tool and filter by the Support department.",
    )

    answer = _answer_of_the_finished_turn(page, MULTI_STEP_RESPONSE_TIMEOUT_MS)
    expect(answer).to_contain_text("Dana Whitfield", timeout=SETTLE_MS)


# --- the waiting helper itself: a browser, but no stack and no model --------------------------------
# Each test builds the three states the real page can be in with `set_content`, so the helper's
# polarity and its failure message are checked directly. Without these, a helper that returned
# immediately (or never) would only show up as a mysteriously fast or slow live test.

# Two assistant bubbles, as on a real page that has already had one turn: the answer to this turn is the LAST.
_PAGE = """
<button id="send" disabled>Send</button>
<div class="msg assistant"><span class="answer-text">the previous turn's answer</span></div>
<div class="msg assistant"><span class="answer-text">the answer</span></div>
"""
_SOON_MS = 300  # the simulated turn ends this long after the helper starts waiting


def test_the_helper_returns_as_soon_as_the_page_re_enables_send(page: Page):
    page.set_content(_PAGE)
    page.evaluate(f"setTimeout(() => document.getElementById('send').disabled = false, {_SOON_MS})")

    started = time.monotonic()
    answer = _answer_of_the_finished_turn(page, timeout_ms=30_000)

    assert time.monotonic() - started < 5  # it did not sit out the 30 s budget
    expect(answer).to_have_text("the answer")


def test_the_helper_reports_a_pause_for_approval_by_tool_name_instead_of_waiting_for_text(page: Page):
    page.set_content(_PAGE)  # Send stays disabled: a paused turn never re-enables it
    page.evaluate(
        f"""setTimeout(() => {{
            const box = document.createElement('div');
            box.className = 'approval';
            box.innerHTML = '<div class="approval-question">⏸ Approve this action? remember({{"text": "dark roast"}})</div>';
            document.body.appendChild(box);
        }}, {_SOON_MS})"""
    )

    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=r"paused for approval instead of answering: .*remember\("):
        _answer_of_the_finished_turn(page, timeout_ms=30_000)
    assert time.monotonic() - started < 5


def test_the_helper_keeps_waiting_while_the_turn_is_still_running(page: Page):
    page.set_content(_PAGE)  # Send disabled and no approval box: the turn is mid-flight
    with pytest.raises(playwright_sync_api.TimeoutError, match="Timeout 1500ms exceeded"):  # the budget it was GIVEN
        _answer_of_the_finished_turn(page, timeout_ms=1_500)
