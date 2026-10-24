"""A message that contains a link, in a domain that cannot open one (GRAPH_PATTERNS.md pattern 79).

Langfuse trace `889b5405`: "run a subAgent to check what this is https://jsonviewer.stack.hu/" was
answered with a confident description of a page nothing had fetched. Ecorp's tools are a knowledge-base
search, a calculator, a staff directory, notes and memory. A sentence in the system prompt ("You cannot
open web pages or links") did nothing for it, measured: 6,000 tokens behind the question, the 7b still
described the page. The same sentence as a short reminder right behind the question made it say it
cannot open links, with the unrelated retrieved chunk in the prompt and without it. So it is a
reminder, like the others, and it is shown only when the question has a link and the domain has no tool
that reaches an address, so it is never a false statement about a domain that can.
"""
import re

# The tools, across the four domains, whose job is to reach an address outside the app.
LINK_TOOL_NAMES = frozenset({"fetch_external_reference", "check_vendor_status_page", "enrich_lead_from_website"})

LINK_REMINDER = (
    "Reminder: you cannot open links. If the person gave you one, say you cannot open it; "
    "do not describe what is on the page or guess what it is."
)

_LINK_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)


def mentions_link(text: str) -> bool:
    return bool(_LINK_RE.search(text))


def cannot_open_links(tool_names: frozenset[str] | set[str]) -> bool:
    """True when none of `tool_names` reaches an address."""
    return not (set(tool_names) & LINK_TOOL_NAMES)
