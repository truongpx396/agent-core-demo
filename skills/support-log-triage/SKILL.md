---
name: support-log-triage
description: Parse a customer-pasted error log, stack trace, or webhook/JSON payload for real facts — occurrence counts, exception types, whether JSON is even well-formed — instead of eyeballing raw pasted text. Use whenever a customer pastes a log dump, error message, or payload and asks what it means or how often something happened.
domains: [support]
---

# Support Log Triage

Don't eyeball a pasted log and guess — get real numbers before you answer
or open a ticket.

1. Pass the customer's pasted text to `run_python_in_sandbox` as part of a
   real script — the `script` argument takes your full Python source
   directly, quotes and apostrophes included, no shell involved at all.
   A short parsing script is usually enough:
   ```python
   import re, json

   text = """<paste the customer's log/payload here, unmodified>"""

   # for a log: count occurrences of the error code they're asking about
   print(len(re.findall(r"db_timeout", text)), "occurrences")

   # for a JSON payload: confirm it's actually well-formed before saying
   # anything about its contents
   try:
       json.loads(text)
       print("valid JSON")
   except json.JSONDecodeError as exc:
       print(f"invalid JSON: {exc}")
   ```
2. If the customer also linked a relevant doc or status page,
   `fetch_external_reference` crawls it live — compare a specific
   value from THAT page (a rate limit, a version number) against what
   the script found in their payload, rather than comparing by eye.
3. If you want to know whether this exact error has come up in other
   tickets before adding a comment or opening a new one, that read-only
   history lookup is exactly what the ticket-researcher subagent
   (run_subagent) already exists for — don't repeat the search yourself.
4. Report the real numbers plainly in your answer (the actual count, the
   actual validation result) — not "I looked into it," the specific
   figures.

If the knowledge base or a ticket already answers this, you don't need any
of the above — search_docs/check_ticket_status first, this is for when the
customer's own pasted data is the thing that needs analyzing.
