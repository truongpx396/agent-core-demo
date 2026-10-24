"""Bound what a tool hands the model.

A sandbox command can print a megabyte, a remote MCP server can return anything, and whatever a tool returns goes into the model's
context and its bill (specs/009 A3: a one-megabyte stdout came back as a 1,000,021-character string). The cap keeps the head AND the
tail: the beginning says what ran, and the end of a command's output, a log or a stack trace is usually where the failure is, so
cutting only the end would drop the part a person needs most.

The marker says how much was left out, so neither the model nor a person reading the trace mistakes a cut result for a whole one.
Every cut is counted (`agent_tool_output_truncated_total`), so a cap that is too small for what a tool legitimately returns shows
up as a rate on that dependency, not as answers that are quietly wrong.

This bounds TEXT that is about to reach the model. It must not be applied to a structured payload another piece of code parses
(the sandbox bridge's JSON): cutting that in the middle leaves it unparseable. Bound the parts of it that go on to the model instead.
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
