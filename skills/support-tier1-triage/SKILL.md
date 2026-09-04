---
name: support-tier1-triage
description: Triage an inbound customer support request end to end — check the knowledge base first, open a ticket if unresolved, escalate to a human when it's outside Tier-1 scope. Use for any customer issue, complaint, or question arriving through the support copilot.
domains: [support]
---

# Tier-1 Support Triage

Work through an inbound customer request in this order — don't skip
straight to opening a ticket if the knowledge base can answer it, and
don't keep guessing once something is clearly outside Tier-1 scope.

1. Call `search_docs` with the customer's question. If it returns a clear,
   on-topic answer, reply directly with citations — no ticket needed.
2. If the request is genuinely ambiguous (could reasonably mean two
   different things), call `ask_clarification` with 2-4 concrete options
   instead of guessing which one the customer meant.
3. If the knowledge base doesn't resolve it, call `create_ticket` with a
   clear subject/description and an honest priority (`urgent` only for
   something actually blocking the customer right now).
4. If the request is something Tier-1 has no tool for at all — a refund,
   an account or billing change, or the customer explicitly asking for a
   person — call `escalate_to_human` on the ticket with a specific reason.
   Never tell the customer you've done something you have no tool for;
   escalate instead.
5. If the customer is asking about a specific ticket they already have, use
   `check_ticket_status`; if they're asking about their tickets generally
   ("what have I reported?") without a number, use `list_my_tickets`
   instead.
6. If the customer is following up on a ticket they already opened with
   more detail rather than a new issue, use `add_ticket_comment` on that
   ticket instead of opening a duplicate one.

Always tell the customer plainly what happened — "I've opened ticket #N"
or "I've escalated this to a specialist" — never leave them guessing
whether anything was actually done.
