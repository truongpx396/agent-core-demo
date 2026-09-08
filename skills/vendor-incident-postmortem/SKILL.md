---
name: vendor-incident-postmortem
description: Investigate whether an anomaly is caused by an external vendor/dependency, with real evidence — not a guess. Use when fetch_metrics_summary flags something and a vendor is a plausible cause, or when someone directly asks "is this our fault or theirs."
domains: [ops]
---

# Vendor Incident Postmortem

A real investigation, not a single tool call — work through it in order,
and don't log an incident on a hunch when the evidence doesn't support it.

1. Call `fetch_metrics_summary` first, if you haven't already — confirm
   there's an actual anomaly before investigating a vendor at all.
2. Call `check_vendor_status_page` on the vendor's public status page.
   This is a live crawl — real page content back, not a summary.
3. Turn that crawled text into real numbers with `run_command_in_sandbox`
   — don't eyeball it. A short parsing script is usually enough:
   ```python
   import re
   text = """<paste the relevant part of the crawled page here>"""
   # count incident mentions, or extract dates/durations with re.findall,
   # depending on how the page is structured
   print(len(re.findall(r"Incident", text)), "incidents mentioned")
   ```
   Adjust the pattern to what the actual page looks like — status pages
   vary a lot in format, so read what you got back before assuming a
   structure.
4. In parallel with step 3 (or right after), delegate to
   `vendor-history-researcher` (`run_subagent`) with the vendor's name —
   this checks OUR OWN incident log for past mentions, a different
   question from step 1's "what does current telemetry look like."
5. Weigh both pieces of evidence together:
   - Vendor's own page shows a real, current incident AND it lines up
     with our own anomaly's timing → this is very likely their fault.
     `log_incident` with BOTH pieces of evidence cited (the vendor's own
     numbers from step 3, and the historical pattern from step 4 if any).
   - Vendor's page shows nothing notable, or the timing doesn't line up →
     don't blame them speculatively; keep investigating our own side, or
     say plainly you couldn't find a vendor-side cause.
6. Only call `post_to_team_channel` if explicitly asked to notify the
   team — logging the incident is not the same as posting about it.

Never write "this is probably their fault" without the specific numbers
from step 3 or the specific incident numbers from step 4 backing it up.
