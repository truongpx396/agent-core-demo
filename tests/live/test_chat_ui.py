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
"""
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

RESPONSE_TIMEOUT_MS = 90_000


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
