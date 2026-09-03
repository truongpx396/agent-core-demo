---
name: sales-lead-qualification
description: Work an inbound lead from first contact through qualification to a clean outcome — scheduled follow-up, handoff to a human rep, or marked lost. Use for any inbound sales/CRM message, not just ones that already show obvious buying intent.
domains: [sales]
---

# Sales Lead Qualification

Work an inbound lead interaction in this order — don't skip straight to a
handoff, and don't let a lead go stale with no next step.

1. Call `log_lead_interaction` first, always — this is how the lead gets
   created or its history extended. Do this before deciding anything else.
2. Call `list_pending_followups` for this lead's contact before scheduling
   a new one, so you never stack a second follow-up on top of one already
   pending.
3. Judge intent from what they actually said:
   - **Real buying intent** (asks about pricing, timeline, wants a demo,
     explicitly asks for a person): call `package_lead_brief`, then
     `handoff_to_human` with a specific reason. Don't keep going back and
     forth once a lead is ready for a person.
   - **Not ready yet, but still interested**: use `schedule_followup` with
     a concrete note about what to say/ask next time.
   - **Clearly not converting** (explicitly not interested, unsubscribes,
     or unresponsive after repeated follow-ups): call `mark_lead_lost`
     with a specific reason. This also cancels any pending follow-ups for
     them, so leave it to this tool rather than trying to cancel manually.
4. Use `search_docs` for any product/company fact you're not sure of —
   never guess at pricing or feature details in a drafted reply.
5. Use `ask_clarification` when the lead's intent is genuinely ambiguous
   (could reasonably be read as either still-interested or a brush-off)
   rather than guessing which one it is.

You have no tool that sends anything to the lead directly — every reply
you draft is for a human rep to review and send.
