"""Bound what a tool hands the model. A sandbox command can print a megabyte and a remote MCP server can return
anything, and whatever a tool returns goes into the model's context and bill (specs/009 A3: a one-megabyte
stdout came back as a 1,000,021-character string). The cap keeps the head AND the tail: the start says what
ran, and the end of command output, a log or a stack trace is usually where the failure is.

The marker says how much was left out, so neither the model nor a reader of the trace mistakes a cut result
for a whole one. Every cut is counted (`agent_tool_output_truncated_total`), so a cap too small for what a
tool legitimately returns shows as a rate on that dependency, not as quietly wrong answers.

This bounds TEXT about to reach the model. Don't apply it to a structured payload another piece of code parses
(the sandbox bridge's JSON): cutting it mid-way leaves it unparseable. Bound the parts that go on to the model
instead.
"""
from app.core import metrics

HEAD_SHARE = 2 / 3  # of the characters kept; the rest is the tail


def bound_output(text: str, limit: int, *, dependency: str) -> str:
    """`text` unchanged if it fits in `limit` characters, else its head and tail with a marker between (`limit` counts the kept
    characters, not the marker). `dependency` names what produced it, for the truncation counter."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * HEAD_SHARE)
    tail = limit - head
    omitted = len(text) - limit
    metrics.agent_tool_output_truncated_total.labels(dependency=dependency).inc()
    return f"{text[:head]}\n… [{omitted} of {len(text)} characters omitted] …\n{text[len(text) - tail:]}"
