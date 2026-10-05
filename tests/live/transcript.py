"""What the web UI showed, as text, for the report of a failed e2e test.

An e2e failure used to say only what one locator held ("expected text 'Dana Whitfield'; actual: ..."), which is
not enough to tell a model that answered wrongly from one that called the wrong tool, paused for approval, or
never started. The page already renders all of it: the user's message, each tool call with its arguments, the
approval box, the status line and the answer. The browser is the only place those arguments exist (the server
logs only a fingerprint of them, on purpose), so a failed test is the one chance to see them.

Best effort by design: whatever calls this must never let it replace the failure it is describing.
"""
from typing import Any

# `.tool-activity-line` text is "🔧 tool({args})" while running and "✓ tool({args})" once done (index.html:
# addToolActivityLine / markToolActivityDone); `.approval-question` is the Approve/Reject box's own label.
_TRANSCRIPT_JS = """() => [...document.querySelectorAll('#messages .msg')].map((message) => {
  const text = (node) => (node ? node.textContent.trim() : '');
  if (!message.classList.contains('assistant')) {
    const role = message.classList.contains('user') ? 'user' : 'system';
    return role + ': ' + text(message);
  }
  const answer = [...message.querySelectorAll('.answer-text')].map(text).filter(Boolean).join(' ');
  const status = text(message.querySelector('.status'));
  const lines = ['assistant: ' + (answer || '(no answer text)') + (status ? ' [status: ' + status + ']' : '')];
  for (const line of message.querySelectorAll('.tool-activity-line')) lines.push('  ' + text(line));
  for (const box of message.querySelectorAll('.approval-question')) lines.push('  ' + text(box));
  return lines.join('\\n');
}).join('\\n')"""


def page_transcript(page: Any) -> str:
    """The conversation on the page, one message per block, an assistant message followed by its tool calls."""
    return page.evaluate(_TRANSCRIPT_JS) or "(no messages on the page)"
