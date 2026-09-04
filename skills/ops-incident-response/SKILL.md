---
name: ops-incident-response
description: Run a full ops investigation end to end — pull current metrics, check whether it's happened before, log a real anomaly as an incident, and resolve it once it's fixed. Use for "why is X happening", "did anything break", or any request to investigate this app's own operational health.
domains: [ops]
---

# Ops Incident Response

Work an investigation in this order — don't log an incident for a routine
check, and don't leave a real one undocumented.

1. Call `fetch_metrics_summary` first. If nothing is flagged as an anomaly
   (past its alert-matching threshold), say so plainly and stop — not
   every check needs an incident.
2. If something IS flagged, call `list_recent_incidents` (status `open` is
   usually enough) to see whether this is already being tracked or has
   happened before. Don't log a duplicate for something already open.
3. If it's new, call `log_incident` with a short summary and the specific
   numbers behind it — cite the actual metric values, never a vague
   description.
4. Only call `post_to_team_channel` if explicitly asked to notify the
   team; logging the incident is not the same as posting about it.
5. When a human later confirms an incident is fixed (or explains it's no
   longer a concern), call `resolve_incident` with what actually resolved
   it — don't leave incidents open once they're addressed.

Never speculate about a cause the numbers don't support — cite which
specific reading led to your conclusion.
